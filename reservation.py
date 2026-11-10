"""SQLite storage and transactional reservation rules."""

import hashlib
import ipaddress
import os
import re
import secrets
import sqlite3
import stat
import string
import tempfile
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SLOT = 1800
DAY = 48 * SLOT
BEIJING = timezone(timedelta(hours=8))
SESSION_SECONDS = 12 * 3600
HISTORY_PAGE_SIZE = 100
USERNAME = re.compile(r"[a-zA-Z0-9_-]{1,24}")
DEFAULT_ADMINS = ("zhaoming", "leixuewei")
DPAPI_KEY_PREFIX = b"NPU-DPAPI\x01"
PASSWORD_ACTIONS = {"create-account", "change-password", "reset-password"}

VISIBLE_MACHINES_SQL = (
    "SELECT m.id FROM machines m WHERE m.retired=0 AND "
    "(EXISTS (SELECT 1 FROM machine_grants d WHERE d.machine_id=m.id AND d.account_id=?) "
    "OR EXISTS (SELECT 1 FROM group_grants g WHERE g.group_id=m.group_id AND g.account_id=?))"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password TEXT NOT NULL,
    password_ciphertext BLOB,
    role TEXT NOT NULL CHECK(role IN ('admin','user')),
    status TEXT NOT NULL DEFAULT 'approved' CHECK(status IN ('pending','approved','rejected')),
    registered_at INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    ip TEXT NOT NULL,
    spec TEXT NOT NULL,
    group_id INTEGER REFERENCES groups(id),
    retired INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS active_machine_ip ON machines(ip) WHERE retired=0;
CREATE TABLE IF NOT EXISTS group_grants (
    account_id INTEGER REFERENCES accounts(id),
    group_id INTEGER REFERENCES groups(id),
    PRIMARY KEY(account_id, group_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS machine_grants (
    account_id INTEGER REFERENCES accounts(id),
    machine_id INTEGER REFERENCES machines(id),
    PRIMARY KEY(account_id, machine_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL REFERENCES machines(id),
    start INTEGER NOT NULL CHECK(start % 1800 = 0),
    end INTEGER NOT NULL CHECK(end % 1800 = 0 AND end > start),
    kind TEXT NOT NULL CHECK(kind IN ('booked','locked')),
    owner_id INTEGER REFERENCES accounts(id),
    reason TEXT NOT NULL DEFAULT '',
    operator_id INTEGER REFERENCES accounts(id),
    CHECK((kind='booked' AND owner_id IS NOT NULL) OR
          (kind='locked' AND owner_id IS NULL))
);
CREATE INDEX IF NOT EXISTS interval_machine_time ON intervals(machine_id, start, end);
CREATE INDEX IF NOT EXISTS interval_end ON intervals(end);
CREATE INDEX IF NOT EXISTS interval_history ON intervals(start DESC,id DESC) WHERE kind='booked';
CREATE TRIGGER IF NOT EXISTS interval_no_overlap
BEFORE INSERT ON intervals WHEN EXISTS (
    SELECT 1 FROM intervals
    WHERE machine_id=NEW.machine_id AND start<NEW.end AND end>NEW.start
)
BEGIN SELECT RAISE(ABORT,'interval_overlap'); END;
CREATE TRIGGER IF NOT EXISTS interval_update_no_overlap
BEFORE UPDATE OF machine_id,start,end ON intervals WHEN EXISTS (
    SELECT 1 FROM intervals
    WHERE id<>OLD.id AND machine_id=NEW.machine_id AND start<NEW.end AND end>NEW.start
)
BEGIN SELECT RAISE(ABORT,'interval_overlap'); END;
CREATE TABLE IF NOT EXISTS metadata (
    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO metadata VALUES (1,0);
"""

MUTATIONS = {
    "create-account": ("create_account", "admin"),
    "review-registrations": ("review_registrations", "admin"),
    "change-password": ("change_password", None),
    "reset-password": ("reset_password", "admin"),
    "add-machine": ("add_machine", "admin"),
    "remove-machine": ("remove_machine", "admin"),
    "create-group": ("create_group", "admin"),
    "edit-group": ("edit_group", "admin"),
    "set-access": ("set_access", "admin"),
    "lock": ("lock", "admin"),
    "unlock": ("unlock", "admin"),
    "book": ("book", "user"),
    "release": ("release", "user"),
}
REVIEWED = {"remove-machine", "edit-group", "set-access", "lock", "unlock"}


class Error(Exception):
    def __init__(self, status, message, code="invalid"):
        super().__init__(message)
        self.status = status
        self.code = code


def text(data, key, maximum, *, required=True):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise Error(422, f"{key} 必须为文本。")
    value = value.strip()
    if len(value) > maximum or (required and not value):
        raise Error(422, f"{key} 需要填写 1–{maximum} 个字符。" if required else f"{key} 最多 {maximum} 个字符。")
    return value


def integer(data, key, *, minimum=0):
    value = data.get(key)
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise Error(422, f"{key} 必须为有效整数。")
    return value


def ids(data, key, valid_ids):
    values = data.get(key)
    if not isinstance(values, list) or any(type(i) is not int or i not in valid_ids for i in values):
        raise Error(422, "请选择有效的机器或分组。")
    return set(values)


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()


def encode_password(password, *, minimum=8):
    if not isinstance(password, str) or not minimum <= len(password) <= 128:
        raise Error(422, f"密码需要 {minimum}–128 个字符。")
    salt = secrets.token_bytes(16)
    return salt.hex() + ":" + password_hash(password, salt)


def generated_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def windows_key_data(data, *, protect):
    """Wrap or unwrap a key using Windows DPAPI in the current account."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if protect:
        crypt = crypt32.CryptProtectData
        description = wintypes.LPCWSTR
    else:
        crypt = crypt32.CryptUnprotectData
        description = ctypes.POINTER(wintypes.LPWSTR)
    blob_ptr = ctypes.POINTER(Blob)
    crypt.argtypes = [blob_ptr, description, blob_ptr, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_ptr]
    crypt.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.addressof(buffer))
    output = Blob()
    try:
        # CRYPTPROTECT_UI_FORBIDDEN. Omitting LOCAL_MACHINE binds it to the account.
        if not crypt(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(output)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(buffer, 0, len(buffer))
        if output.data:
            if not protect:
                ctypes.memset(output.data, 0, output.size)
            kernel32.LocalFree(output.data)


def clock_window(now):
    local = datetime.fromtimestamp(now, BEIJING)
    day = local.replace(hour=8, minute=0, second=0, microsecond=0)
    start = int(day.timestamp())
    cutoff = -(-now // SLOT) * SLOT
    return start, cutoff


class Store:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.key_path = Path(os.environ.get("NPU_RESERVE_PASSWORD_KEY_FILE") or f"{self.path}.key")

    @contextmanager
    def transaction(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        finally:
            db.close()
        with self.transaction(write=True) as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(intervals)")}
            if "operator_id" not in columns:
                db.execute("ALTER TABLE intervals ADD COLUMN operator_id INTEGER REFERENCES accounts(id)")
            account_columns = {row["name"] for row in db.execute("PRAGMA table_info(accounts)")}
            if "password_ciphertext" not in account_columns:
                db.execute("ALTER TABLE accounts ADD COLUMN password_ciphertext BLOB")
            if "status" not in account_columns:
                db.execute("ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'approved' "
                           "CHECK(status IN ('pending','approved','rejected'))")
            if "registered_at" not in account_columns:
                db.execute("ALTER TABLE accounts ADD COLUMN registered_at INTEGER")
            self.cipher = self.load_password_cipher(db)
            for username in DEFAULT_ADMINS:
                account = db.execute("SELECT id,role FROM accounts WHERE username=?", (username,)).fetchone()
                if account is None:
                    encoded = encode_password(username, minimum=1)
                    self.insert_account(db, {"username": username}, encoded, "admin")
                elif account["role"] != "admin":
                    db.execute("UPDATE accounts SET role='admin',status='approved',password_ciphertext=NULL WHERE id=?", (account["id"],))
                    db.execute("DELETE FROM sessions WHERE account_id=?", (account["id"],))
                else:
                    continue
                self.bump(db)

    def load_password_cipher(self, db):
        if not self.key_path.exists():
            if db.execute("SELECT 1 FROM accounts WHERE password_ciphertext IS NOT NULL LIMIT 1").fetchone():
                raise RuntimeError(f"找不到用户密码密钥，请恢复密钥文件 {self.key_path}")
            self.create_password_key()
        with self.key_path.open("rb") as key:
            if os.name != "nt":
                self.check_key_permissions(key)
            data = key.read()
        if os.name == "nt":
            if not data.startswith(DPAPI_KEY_PREFIX):
                raise RuntimeError("当前密钥文件不是 Windows DPAPI 格式。请在创建该密钥的环境中启动服务。")
            try:
                data = windows_key_data(data[len(DPAPI_KEY_PREFIX):], protect=False)
            except OSError as error:
                raise RuntimeError("无法解开 Windows 密钥。请使用创建密钥的 Windows 账户启动服务。") from error
        return Fernet(data)

    def create_password_key(self):
        self.key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.key_path.parent, delete=False) as key:
                temporary = Path(key.name)
                if os.name == "nt":
                    data = DPAPI_KEY_PREFIX + windows_key_data(Fernet.generate_key(), protect=True)
                else:
                    self.check_key_permissions(key)
                    data = Fernet.generate_key()
                key.write(data)
                key.flush()
                os.fsync(key.fileno())
            try:
                if os.name == "nt":
                    # On Windows rename fails if the destination exists.
                    temporary.rename(self.key_path)
                else:
                    os.link(temporary, self.key_path)
            except FileExistsError:
                pass
            if os.name != "nt":
                directory = os.open(self.key_path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if temporary is not None:
                with suppress(FileNotFoundError):
                    temporary.unlink()

    @staticmethod
    def check_key_permissions(file):
        mode = os.fstat(file.fileno()).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
            raise RuntimeError("密码密钥必须是仅服务账户可访问的文件。可用 NPU_RESERVE_PASSWORD_KEY_FILE 指定支持 0600 权限的路径。")

    def view_password(self, token, data):
        with self.transaction() as db:
            actor = self.actor(db, token, int(self.clock()))
            if actor["role"] != "admin":
                raise Error(403, "没有查看用户密码的权限。")
            account = db.execute(
                "SELECT password_ciphertext FROM accounts WHERE id=? AND role='user' AND status='approved'", (integer(data, "id"),)
            ).fetchone()
            if account is None:
                raise Error(404, "普通用户不存在。")
            if account["password_ciphertext"] is None:
                raise Error(409, "该用户的密码尚不可查看。用户重新登录、修改密码或管理员重置后即可查看。")
            try:
                return {"password": self.cipher.decrypt(account["password_ciphertext"]).decode()}
            except InvalidToken:
                raise Error(503, "无法解密该用户的密码，请检查服务的密码密钥。") from None

    def bootstrap(self, username, name, password):
        encoded = encode_password(password)
        with self.transaction(write=True) as db:
            self.insert_account(db, {"username": username, "name": name}, encoded, "admin")
            self.bump(db)

    @staticmethod
    def insert_account(db, data, encoded, role):
        username = text(data, "username", 24)
        if not USERNAME.fullmatch(username):
            raise Error(422, "登录账户只允许字母、数字、下划线和连字符。")
        name = text(data, "name", 24, required=False) or username
        if db.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
            raise Error(409, "登录账户已存在。")
        cursor = db.execute(
            "INSERT INTO accounts(username,name,password,role) VALUES (?,?,?,?)",
            (username, name, encoded, role),
        )
        return cursor.lastrowid

    @staticmethod
    def registration_message(db, status):
        names = [row[0] for row in db.execute("SELECT name FROM accounts WHERE role='admin' ORDER BY id")]
        headline = "等待审核" if status == "pending" else "注册申请未通过"
        return f"{headline}，请联系管理员 {' / '.join(names)}。"

    def register(self, data):
        generated = generated_password() if data.get("generate") is True else None
        password = generated or data.get("password")
        encoded = encode_password(password)
        ciphertext = self.cipher.encrypt(password.encode())
        with self.transaction(write=True) as db:
            target = self.insert_account(db, data, encoded, "user")
            db.execute("UPDATE accounts SET status='pending',registered_at=?,password_ciphertext=? WHERE id=?",
                       (int(self.clock()), ciphertext, target))
            self.bump(db)
            result = {"message": self.registration_message(db, "pending")}
            if generated is not None:
                result["generatedPassword"] = generated
            return result

    def login(self, data, previous=None):
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 24 or len(password) > 128:
            raise Error(401, "账户或密码有误。")
        with self.transaction() as db:
            account = db.execute("SELECT * FROM accounts WHERE username=?", (username.strip(),)).fetchone()
        salt, expected = account["password"].split(":") if account else ("00" * 16, "")
        matches = secrets.compare_digest(password_hash(password, bytes.fromhex(salt)), expected)
        if not account or not matches:
            raise Error(401, "账户或密码有误。")
        token = secrets.token_urlsafe(32)
        with self.transaction(write=True) as db:
            current = db.execute("SELECT * FROM accounts WHERE id=?", (account["id"],)).fetchone()
            if current is None or current["password"] != account["password"]:
                raise Error(401, "账户或密码有误。")
            if current["status"] != "approved":
                raise Error(403, self.registration_message(db, current["status"]),
                            f"registration_{current['status']}")
            if current["role"] == "user" and current["password_ciphertext"] is None:
                db.execute("UPDATE accounts SET password_ciphertext=? WHERE id=?",
                           (self.cipher.encrypt(password.encode()), current["id"]))
            now = int(self.clock())
            db.execute("DELETE FROM sessions WHERE expires<=? OR token=?", (now, token_hash(previous or "")))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (token_hash(token), account["id"], now + SESSION_SECONDS))
            state = self.snapshot(db, current, now)
        return token, state

    def logout(self, token):
        if token:
            with self.transaction(write=True) as db:
                db.execute("DELETE FROM sessions WHERE token=?", (token_hash(token),))

    @staticmethod
    def actor(db, token, now):
        row = db.execute(
            "SELECT a.id,a.username,a.name,a.role FROM sessions s JOIN accounts a ON a.id=s.account_id "
            "WHERE s.token=? AND s.expires>? AND a.status='approved'", (token_hash(token or ""), now),
        ).fetchone()
        if row is None:
            raise Error(401, "请先登录。", "login_required")
        return row

    @staticmethod
    def revision(db):
        return db.execute("SELECT revision FROM metadata WHERE id=1").fetchone()[0]

    @staticmethod
    def bump(db):
        db.execute("UPDATE metadata SET revision=revision+1 WHERE id=1")

    @staticmethod
    def visible(db, account):
        return {
            row[0] for row in db.execute(
                VISIBLE_MACHINES_SQL,
                (account, account),
            )
        }

    def state(self, token):
        with self.transaction() as db:
            now = int(self.clock())
            return self.snapshot(db, self.actor(db, token, now), now)

    def history(self, token, page):
        if not 1 <= page <= 1_000_000:
            raise Error(422, "历史页码无效。")
        with self.transaction() as db:
            now = int(self.clock())
            actor = self.actor(db, token, now)
            scope, params = "", [now]
            if actor["role"] != "admin":
                scope = f" AND (i.owner_id=? OR i.machine_id IN ({VISIBLE_MACHINES_SQL}))"
                params.extend([actor["id"]] * 3)
            rows = db.execute(
                "SELECT i.id,i.start,i.end,m.name AS machine,m.ip,m.retired,a.name "
                "FROM intervals i JOIN machines m ON m.id=i.machine_id JOIN accounts a ON a.id=i.owner_id "
                "WHERE i.kind='booked' AND i.start<?" + scope + " ORDER BY i.start DESC,i.id DESC LIMIT ? OFFSET ?",
                (*params, HISTORY_PAGE_SIZE + 1, (page - 1) * HISTORY_PAGE_SIZE),
            ).fetchall()
            return {"to": now, "page": page, "hasMore": len(rows) > HISTORY_PAGE_SIZE,
                    "records": [dict(row) for row in rows[:HISTORY_PAGE_SIZE]]}

    def snapshot(self, db, actor, now):
        start, cutoff = clock_window(now)
        admin = actor["role"] == "admin"
        machines = [dict(row) for row in db.execute("SELECT id,name,ip,spec,group_id FROM machines WHERE retired=0 ORDER BY id")]
        if not admin:
            visible = self.visible(db, actor["id"])
            machines = [m for m in machines if m["id"] in visible]
        groups = [
            {"id": row["id"], "name": row["name"], "members": [m["id"] for m in machines if m["group_id"] == row["id"]]}
            for row in db.execute("SELECT id,name FROM groups ORDER BY id")
        ]
        accounts, administrators, registrations = [], [], []
        if admin:
            administrators = [dict(row) for row in db.execute(
                "SELECT id,name,username FROM accounts WHERE role='admin' ORDER BY id"
            )]
            accounts = [dict(row) for row in db.execute(
                "SELECT id,name,username FROM accounts WHERE role='user' AND status='approved' ORDER BY id"
            )]
            registrations = [dict(row) for row in db.execute(
                "SELECT id,name,username,registered_at FROM accounts WHERE role='user' AND status='pending' "
                "ORDER BY registered_at,id"
            )]
            group_grants = list(db.execute("SELECT account_id,group_id FROM group_grants ORDER BY group_id"))
            direct_grants = list(db.execute("SELECT account_id,machine_id FROM machine_grants ORDER BY machine_id"))
            for account in accounts:
                account["groups"] = [row[1] for row in group_grants if row[0] == account["id"]]
                account["direct"] = [row[1] for row in direct_grants if row[0] == account["id"]]
        else:
            groups = [g for g in groups if g["members"]]
        visible_ids = {m["id"] for m in machines}
        intervals = db.execute(
            "SELECT i.*,a.username,a.name AS owner_name,op.name AS operator_name FROM intervals i "
            "LEFT JOIN accounts a ON a.id=i.owner_id LEFT JOIN accounts op ON op.id=i.operator_id "
            "WHERE i.start<? AND i.end>? ORDER BY i.start,i.id", (start + DAY, min(start, cutoff)),
        )
        records, upcoming = [], []
        for row in intervals:
            if row["machine_id"] not in visible_ids:
                continue
            record = {
                "id": row["id"], "m": row["machine_id"], "s": (row["start"] - start) // SLOT,
                "e": (row["end"] - start) // SLOT, "kind": row["kind"], "reason": row["reason"],
                "operator": row["operator_name"] if row["kind"] == "locked" else row["owner_name"],
            }
            if admin or row["owner_id"] == actor["id"]:
                record["owner"] = row["username"]
            if row["end"] > start:
                records.append(record)
            if row["end"] > cutoff and (admin or row["owner_id"] == actor["id"]):
                upcoming.append(record)
        return {
            "user": {k: actor[k] for k in ("id", "username", "name", "role")},
            "machines": machines, "groups": groups, "accounts": accounts, "administrators": administrators, "records": records,
            "registrations": registrations, "upcoming": upcoming, "window": start, "cutoff": (cutoff - start) // SLOT,
            "pastEnd": max(0, (cutoff - start) // SLOT), "revision": self.revision(db),
        }

    def mutate(self, token, action, data):
        if action not in MUTATIONS:
            raise Error(404, "接口不存在。")
        method, role = MUTATIONS[action]
        # Hash outside the write transaction after a cheap authorization check.
        generated = None
        if action in PASSWORD_ACTIONS:
            with self.transaction() as db:
                actor = self.actor(db, token, int(self.clock()))
                if role and actor["role"] != role:
                    raise Error(403, "没有执行此操作的权限。")
            if action != "change-password" and data.get("generate") is True:
                generated = generated_password()
            password = generated or data.get("password")
            data = {**data, "encoded": encode_password(password), "password_bytes": password.encode(),
                    "session_hash": token_hash(token)}
        with self.transaction(write=True) as db:
            now = int(self.clock())
            actor = self.actor(db, token, now)
            if role and actor["role"] != role:
                raise Error(403, "没有执行此操作的权限。")
            if action in REVIEWED and integer(data, "revision") != self.revision(db):
                raise Error(409, "数据已变化，已刷新，请核对后重新提交。", "state_changed")
            message = getattr(self, method)(db, actor, data, now)
            self.bump(db)
            result = {"message": message, "state": self.snapshot(db, actor, now)}
            if generated is not None:
                result["generatedPassword"] = generated
            return result

    def create_account(self, db, actor, data, now):
        target = self.insert_account(db, data, data["encoded"], "user")
        db.execute("UPDATE accounts SET password_ciphertext=? WHERE id=?",
                   (self.cipher.encrypt(data["password_bytes"]), target))
        return "账户已创建，可以为用户分配机器权限。"

    def review_registrations(self, db, actor, data, now):
        decision = data.get("decision")
        if decision not in ("approve", "reject"):
            raise Error(422, "请选择通过或拒绝。")
        selection = data.get("ids")
        if not isinstance(selection, list) or not selection or any(type(i) is not int or i <= 0 for i in selection):
            raise Error(422, "请选择要审核的申请。")
        selected = set(selection)
        pending = {row[0] for row in db.execute("SELECT id FROM accounts WHERE role='user' AND status='pending'")}
        if not selected <= pending:
            raise Error(409, "申请状态已更新，请核对列表后重新选择。", "state_changed")
        if decision == "approve":
            db.executemany("UPDATE accounts SET status='approved' WHERE id=?", [(i,) for i in selected])
            return f"已通过 {len(selected)} 条注册申请。"
        db.executemany("UPDATE accounts SET status='rejected',password_ciphertext=NULL WHERE id=?",
                       [(i,) for i in selected])
        return f"已拒绝 {len(selected)} 条注册申请。"

    def set_password(self, db, target, data):
        account = db.execute("SELECT role FROM accounts WHERE id=? AND status='approved'", (target,)).fetchone()
        if account is None:
            raise Error(404, "账户不存在。")
        ciphertext = self.cipher.encrypt(data["password_bytes"]) if account["role"] == "user" else None
        db.execute("UPDATE accounts SET password=?,password_ciphertext=? WHERE id=?",
                   (data["encoded"], ciphertext, target))
        db.execute("DELETE FROM sessions WHERE account_id=? AND token<>?", (target, data["session_hash"]))

    def change_password(self, db, actor, data, now):
        self.set_password(db, actor["id"], data)
        return "密码已修改。"

    def reset_password(self, db, actor, data, now):
        self.set_password(db, integer(data, "id"), data)
        return "密码已重置。"

    @staticmethod
    def add_machine(db, actor, data, now):
        name = text(data, "name", 48)
        raw_ip = text(data, "ip", 45)
        try:
            if "%" in raw_ip:
                raise ValueError
            ip = str(ipaddress.ip_address(raw_ip))
        except ValueError:
            raise Error(422, "请输入有效的 IPv4 或 IPv6 地址。") from None
        spec = text(data, "spec", 80, required=False)
        if db.execute("SELECT 1 FROM machines WHERE ip=? AND retired=0", (ip,)).fetchone():
            raise Error(409, "资源池中已有此 IP 地址。")
        db.execute("INSERT INTO machines(name,ip,spec) VALUES (?,?,?)", (name, ip, spec))
        return f"已新增 · {name} · {ip}"

    @staticmethod
    def machine(db, data):
        machine = db.execute("SELECT * FROM machines WHERE id=? AND retired=0", (integer(data, "m"),)).fetchone()
        if machine is None:
            raise Error(404, "机器已移除或不存在。", "state_changed")
        return machine

    def remove_machine(self, db, actor, data, now):
        machine = self.machine(db, data)
        m = machine["id"]
        _, cutoff = clock_window(now)
        records = db.execute("SELECT * FROM intervals WHERE machine_id=? AND end>?", (m, cutoff)).fetchall()
        for record in records:
            self.cut(db, record, cutoff, record["end"])
        db.execute("DELETE FROM machine_grants WHERE machine_id=?", (m,))
        db.execute("UPDATE machines SET retired=1,group_id=NULL WHERE id=?", (m,))
        return f"已删除 · {machine['name']}"

    @staticmethod
    def create_group(db, actor, data, now):
        name = text(data, "name", 24)
        if db.execute("SELECT 1 FROM groups WHERE name=?", (name,)).fetchone():
            raise Error(409, "组名已存在。")
        db.execute("INSERT INTO groups(name) VALUES (?)", (name,))
        return "分组已创建，请选择组内机器。"

    def edit_group(self, db, actor, data, now):
        group_id = integer(data, "id")
        name = text(data, "name", 24)
        if not db.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone():
            raise Error(404, "分组不存在。")
        if db.execute("SELECT 1 FROM groups WHERE name=? AND id<>?", (name, group_id)).fetchone():
            raise Error(409, "组名已存在。")
        members = ids(data, "members", {row[0] for row in db.execute("SELECT id FROM machines WHERE retired=0")})
        before = {row[0]: self.visible(db, row[0]) for row in db.execute("SELECT id FROM accounts WHERE role='user' AND status='approved'")}
        db.execute("UPDATE groups SET name=? WHERE id=?", (name, group_id))
        db.execute("UPDATE machines SET group_id=NULL WHERE group_id=?", (group_id,))
        db.executemany("UPDATE machines SET group_id=? WHERE id=?", [(group_id, m) for m in members])
        count = self.release_revoked(db, before, now)
        return "分组已保存。" + self.revoked_summary(count)

    def set_access(self, db, actor, data, now):
        account = integer(data, "id")
        if not db.execute("SELECT 1 FROM accounts WHERE id=? AND role='user' AND status='approved'", (account,)).fetchone():
            raise Error(404, "用户不存在。")
        groups = ids(data, "groups", {row[0] for row in db.execute("SELECT id FROM groups")})
        machines = ids(data, "direct", {row[0] for row in db.execute("SELECT id FROM machines WHERE retired=0")})
        before = self.visible(db, account)
        db.execute("DELETE FROM group_grants WHERE account_id=?", (account,))
        db.execute("DELETE FROM machine_grants WHERE account_id=?", (account,))
        db.executemany("INSERT INTO group_grants VALUES (?,?)", [(account, g) for g in groups])
        db.executemany("INSERT INTO machine_grants VALUES (?,?)", [(account, m) for m in machines])
        count = self.release_revoked(db, {account: before}, now)
        return f"权限已保存 · 可见 {len(self.visible(db, account))} 台机器。" + self.revoked_summary(count)

    @staticmethod
    def revoked_summary(count):
        return f"已自动释放 {count} 笔预约的剩余机时。" if count else ""

    def release_revoked(self, db, before, now):
        _, cutoff = clock_window(now)
        count = 0
        for account, visible in before.items():
            for machine in visible - self.visible(db, account):
                records = db.execute(
                    "SELECT * FROM intervals WHERE owner_id=? AND machine_id=? AND end>?",
                    (account, machine, cutoff),
                ).fetchall()
                for record in records:
                    self.cut(db, record, cutoff, record["end"])
                count += len(records)
        return count

    @staticmethod
    def selected_range(data, now, *, overnight=False):
        window, cutoff = clock_window(now)
        if integer(data, "window") != window:
            raise Error(409, "预约窗口已切换，请在更新时间轴后重选。", "state_changed")
        # Previous-window reservations may still be releasable after midnight.
        first = min(0, (cutoff - window) // SLOT) if overnight else 0
        s, e = integer(data, "s", minimum=first), integer(data, "e", minimum=first)
        if not first <= s < e <= 48:
            raise Error(422, "请选择时间轴内有效的起止时间。")
        start, end = window + s * SLOT, window + e * SLOT
        if start < cutoff:
            raise Error(409, "所选时段已经开始，请重新选择。", "state_changed")
        return start, end

    @staticmethod
    def overlaps(db, machine, start, end):
        return db.execute(
            "SELECT * FROM intervals WHERE machine_id=? AND start<? AND end>? ORDER BY start",
            (machine, end, start),
        ).fetchall()

    @staticmethod
    def insert_interval(db, machine, start, end, kind, owner=None, reason="", operator=None):
        db.execute(
            "INSERT INTO intervals(machine_id,start,end,kind,owner_id,reason,operator_id) VALUES (?,?,?,?,?,?,?)",
            (machine, start, end, kind, owner, reason, operator),
        )

    def cut(self, db, record, start, end):
        db.execute("DELETE FROM intervals WHERE id=?", (record["id"],))
        for left, right in ((record["start"], min(start, record["end"])), (max(end, record["start"]), record["end"])):
            if left < right:
                self.insert_interval(
                    db, record["machine_id"], left, right, record["kind"],
                    record["owner_id"], record["reason"], record["operator_id"],
                )

    def book(self, db, actor, data, now):
        machine = self.machine(db, data)
        m = machine["id"]
        if m not in self.visible(db, actor["id"]):
            raise Error(403, "你已无权预约此机器。", "state_changed")
        start, end = self.selected_range(data, now)
        if self.overlaps(db, m, start, end):
            raise Error(409, "预约未成功，所选时段已被预约或锁定。请重新选择。", "occupied")
        self.insert_interval(db, m, start, end, "booked", actor["id"])
        return f"预约成功 · {machine['name']}"

    def release(self, db, actor, data, now):
        record = db.execute(
            "SELECT * FROM intervals WHERE id=? AND owner_id=? AND kind='booked'",
            (integer(data, "id"), actor["id"]),
        ).fetchone()
        if record is None or record["machine_id"] not in self.visible(db, actor["id"]):
            raise Error(409, "预约已变更或不可操作，请重新选择。", "state_changed")
        start, end = self.selected_range(data, now, overnight=True)
        if not record["start"] <= start < end <= record["end"]:
            raise Error(422, "只能释放本人预约范围内的机时。")
        self.cut(db, record, start, end)
        return "已释放，所选时段可重新预约。"

    def lock(self, db, actor, data, now):
        machine = self.machine(db, data)
        m = machine["id"]
        start, end = self.selected_range(data, now)
        overlapping = self.overlaps(db, m, start, end)
        bookings = [record for record in overlapping if record["kind"] == "booked"]
        gaps = []
        cursor = start
        for record in overlapping:
            if record["kind"] == "locked":
                if cursor < record["start"]:
                    gaps.append((cursor, record["start"]))
                cursor = max(cursor, record["end"])
        if cursor < end:
            gaps.append((cursor, end))
        reason = text(data, "reason", 40, required=bool(gaps))
        for record in bookings:
            self.cut(db, record, start, end)
        for left, right in gaps:
            self.insert_interval(db, m, left, right, "locked", reason=reason, operator=actor["id"])
        summary = "已锁定" if gaps else "所选时段已锁定"
        if bookings:
            summary += f"，已强制释放 {len(bookings)} 笔预约的重叠时段"
        return summary + "。"

    def unlock(self, db, actor, data, now):
        machine = self.machine(db, data)
        start, end = self.selected_range(data, now)
        locked = [r for r in self.overlaps(db, machine["id"], start, end) if r["kind"] == "locked"]
        if not locked:
            raise Error(422, "选区内没有锁定。")
        for record in locked:
            self.cut(db, record, start, end)
        return "已解锁，释放的时段可重新预约。"
