import errno
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
from threading import Barrier, Thread

from reservation import BEIJING, DPAPI_KEY_PREFIX, SCHEMA, SLOT, Error, Store, encode_password
from server import Server

PASSWORD = "test-only-password"


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "test.sqlite3"
        self.now = int(datetime(2026, 11, 6, 8, tzinfo=BEIJING).timestamp())
        self.store = Store(self.path, clock=lambda: self.now)
        self.store.initialize()
        self.store.bootstrap("admin", "管理员", PASSWORD)
        self.admin, _ = self.login("admin")
        for username in ("alice", "bob"):
            self.admin_action("create-account", username=username, name=username, password=PASSWORD)
        self.m = self.admin_action("add-machine", name="a5-01", ip="192.0.2.11")["machines"][0]["id"]
        self.group = self.admin_action("create-group", name="A5")["groups"][0]["id"]
        self.admin_action("edit-group", id=self.group, name="A5", members=[self.m])
        self.grant("alice", [self.group], [])
        self.grant("bob", [], [self.m])
        self.alice, _ = self.login("alice")
        self.bob, _ = self.login("bob")

    def login(self, username):
        return self.store.login({"username": username, "password": PASSWORD})

    def admin_action(self, action, **data):
        data.setdefault("revision", self.store.state(self.admin)["revision"])
        return self.store.mutate(self.admin, action, data)["state"]

    def grant(self, username, groups, direct):
        account = next(a for a in self.store.state(self.admin)["accounts"] if a["username"] == username)
        return self.admin_action("set-access", id=account["id"], groups=groups, direct=direct)

    def payload(self, s, e, **extra):
        return {"m": self.m, "window": self.store.state(self.admin)["window"], "s": s, "e": e, **extra}

    def book(self, actor, s, e):
        return self.store.mutate(actor, "book", self.payload(s, e))["state"]

class ReservationTests(Fixture):
    def test_concurrent_overlapping_bookings_have_one_winner(self):
        barrier = Barrier(12)
        payload = self.payload(6, 10)

        def book(token):
            barrier.wait(timeout=10)
            try:
                self.store.mutate(token, "book", payload)
                return 200
            except Error as error:
                self.assertEqual(error.code, "occupied")
                return error.status

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(book, [self.alice, self.bob] * 6))
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 11)
        self.book(self.bob, 10, 11)
        self.assertEqual(len(self.store.state(self.admin)["records"]), 2)

    def test_partial_release_and_rebooking(self):
        state = self.book(self.alice, 4, 12)
        record = state["records"][0]
        self.assertNotIn("owner", self.store.state(self.bob)["records"][0])
        with self.assertRaises(Error):
            self.store.mutate(self.bob, "release", self.payload(6, 8, id=record["id"]))
        self.store.mutate(self.alice, "release", self.payload(6, 8, id=record["id"]))
        self.assertEqual([(r["s"], r["e"]) for r in self.store.state(self.alice)["records"]], [(4, 6), (8, 12)])
        self.book(self.bob, 6, 8)
        with self.assertRaises(Error) as stale:
            self.store.mutate(self.alice, "release", self.payload(8, 12, id=record["id"]))
        self.assertEqual(stale.exception.code, "state_changed")
        remaining = self.store.state(self.alice)["records"][-1]
        self.store.mutate(self.alice, "release", self.payload(8, 12, id=remaining["id"]))
        self.book(self.bob, 8, 12)

    def test_force_lock_and_partial_unlock_preserve_remainders(self):
        self.book(self.alice, 4, 12)
        self.admin_action("lock", **self.payload(14, 18, reason="旧锁"))
        state = self.admin_action("lock", **self.payload(6, 8, reason="切分"))
        self.assertEqual([(r["s"], r["e"]) for r in state["records"] if r["kind"] == "booked"], [(4, 6), (8, 12)])
        self.admin_action("lock", **self.payload(10, 16, reason="维护"))
        state = self.admin_action("unlock", **self.payload(12, 16))
        self.assertEqual([(r["s"], r["e"], r["reason"]) for r in state["records"] if r["kind"] == "locked"],
                         [(6, 8, "切分"), (10, 12, "维护"), (16, 18, "旧锁")])
        self.book(self.bob, 12, 16)

    def test_admin_confirmation_detects_new_booking(self):
        revision = self.store.state(self.admin)["revision"]
        self.book(self.alice, 6, 10)
        with self.assertRaises(Error) as stale:
            self.admin_action("lock", **self.payload(8, 12, reason="维护", revision=revision))
        self.assertEqual(stale.exception.code, "state_changed")
        self.assertEqual([r["kind"] for r in self.store.state(self.admin)["records"]], ["booked"])

    def test_grant_union_then_revocation_and_machine_removal(self):
        self.book(self.alice, 4, 8)
        self.grant("alice", [self.group], [self.m])
        self.admin_action("edit-group", id=self.group, name="A5", members=[])
        self.assertEqual(len(self.store.state(self.alice)["records"]), 1)
        self.grant("alice", [], [])
        self.assertEqual(self.store.state(self.alice)["machines"], [])
        with self.assertRaises(Error) as denied:
            self.book(self.alice, 4, 8)
        self.assertEqual(denied.exception.status, 403)
        self.book(self.bob, 4, 8)
        state = self.admin_action("remove-machine", m=self.m)
        self.assertEqual(next(a for a in state["accounts"] if a["username"] == "bob")["direct"], [])
        self.assertEqual(self.store.state(self.bob)["records"], [])
        state = self.admin_action("add-machine", name="替换机", ip="192.0.2.11")
        self.assertNotEqual(state["machines"][0]["id"], self.m)

    def test_midnight_window_and_release_cutoff(self):
        self.book(self.alice, 30, 48)
        self.now += SLOT * 33 + 1
        self.admin, _ = self.login("admin")
        self.alice, alice = self.login("alice")
        admin = self.store.state(self.admin)
        self.assertEqual(admin["records"], [])
        self.assertEqual(len(admin["upcoming"]), 1)
        record = alice["upcoming"][0]
        self.assertLess(record["s"], 0)
        self.store.mutate(self.alice, "release", self.payload(-13, -11, id=record["id"]))
        self.assertEqual(len(self.store.state(self.alice)["upcoming"]), 2)
        self.grant("alice", [], [])
        with self.store.transaction() as db:
            start, end = db.execute("SELECT start,end FROM intervals").fetchone()
        self.assertEqual(end - start, 4 * SLOT)
        self.assertEqual(self.store.state(self.admin)["upcoming"], [])

    def test_session_permissions_and_persistence(self):
        with self.assertRaises(Error) as denied:
            self.store.mutate(self.alice, "add-machine", {"name": "X", "ip": "192.0.2.33"})
        self.assertEqual(denied.exception.status, 403)
        self.book(self.alice, 8, 9)
        reopened = Store(self.path, clock=lambda: self.now)
        reopened.initialize()
        self.assertEqual(len(reopened.state(self.alice)["records"]), 1)
        self.now += 12 * 3600
        with self.assertRaises(Error) as expired:
            reopened.state(self.alice)
        self.assertEqual(expired.exception.status, 401)

    def test_default_accounts_are_admins_and_restart_preserves_new_password(self):
        for username in ("zhaoming", "leixuewei"):
            _, state = self.store.login({"username": username, "password": username})
            self.assertEqual((state["user"]["role"], state["user"]["name"]), ("admin", username))
        token, _ = self.store.login({"username": "zhaoming", "password": "zhaoming"})
        self.store.mutate(token, "change-password", {"password": PASSWORD})
        self.store.initialize()
        self.login("zhaoming")
        with self.assertRaises(Error):
            self.store.login({"username": "zhaoming", "password": "zhaoming"})

    def test_upgrade_promotes_default_users_and_requires_new_login(self):
        with self.store.transaction(write=True) as db:
            db.execute("UPDATE accounts SET role='user' WHERE username IN ('zhaoming','leixuewei')")
        token, original = self.store.login({"username": "zhaoming", "password": "zhaoming"})
        self.store.mutate(token, "change-password", {"password": PASSWORD})
        self.store.initialize()
        with self.assertRaises(Error) as expired:
            self.store.state(token)
        self.assertEqual(expired.exception.status, 401)
        _, upgraded = self.login("zhaoming")
        self.assertEqual(upgraded["user"]["id"], original["user"]["id"])
        self.assertEqual(upgraded["user"]["role"], "admin")
        _, upgraded_second = self.store.login({"username": "leixuewei", "password": "leixuewei"})
        self.assertEqual(upgraded_second["user"]["role"], "admin")
        self.assertTrue({"zhaoming", "leixuewei"}.isdisjoint(a["username"] for a in upgraded["accounts"]))

    def test_generated_account_password_and_user_self_change(self):
        result = self.store.mutate(self.admin, "create-account", {"username": "new-user", "generate": True})
        initial = result["generatedPassword"]
        self.assertRegex(initial, r"^[A-Za-z0-9]{8}$")
        self.assertNotIn(initial, json.dumps(result["state"]))
        token, state = self.store.login({"username": "new-user", "password": initial})
        other_session, _ = self.store.login({"username": "new-user", "password": initial})
        self.assertEqual(state["user"]["name"], "new-user")
        self.assertEqual(state["administrators"], [])
        with self.assertRaises(Error) as denied:
            self.store.mutate(token, "reset-password", {"id": state["user"]["id"], "generate": True})
        self.assertEqual(denied.exception.status, 403)
        self.store.mutate(token, "change-password", {"password": PASSWORD})
        self.assertEqual(self.store.state(token)["user"]["username"], "new-user")
        with self.assertRaises(Error):
            self.store.state(other_session)
        self.login("new-user")

    def test_admin_can_repeatedly_view_current_user_password(self):
        user_id = self.store.state(self.alice)["user"]["id"]
        target = {"id": user_id}
        self.assertEqual(self.store.view_password(self.admin, target), {"password": PASSWORD})
        with self.assertRaises(Error) as denied:
            self.store.view_password(self.alice, target)
        self.assertEqual(denied.exception.status, 403)
        with self.assertRaises(Error) as admin_not_viewable:
            self.store.view_password(self.admin, {"id": self.store.state(self.admin)["user"]["id"]})
        self.assertEqual(admin_not_viewable.exception.status, 404)
        with self.assertRaises(Error):
            self.store.mutate(self.alice, "change-password", {"password": "1234567"})
        self.store.mutate(self.alice, "change-password", {"password": "12345678"})
        self.assertEqual(self.store.view_password(self.admin, target), {"password": "12345678"})
        result = self.store.mutate(self.admin, "reset-password", {**target, "generate": True})
        password = result["generatedPassword"]
        self.assertRegex(password, r"^[A-Za-z0-9]{8}$")
        reopened = Store(self.path, clock=lambda: self.now)
        reopened.initialize()
        self.assertEqual(reopened.view_password(self.admin, target), {"password": password})
        self.assertEqual(reopened.view_password(self.admin, target), {"password": password})
        with reopened.transaction() as db:
            account = db.execute("SELECT password,password_ciphertext FROM accounts WHERE id=?", (user_id,)).fetchone()
            self.assertNotEqual(account["password"], password)
            self.assertNotEqual(account["password_ciphertext"], password.encode())
        if os.name == "nt":
            self.assertTrue(reopened.key_path.read_bytes().startswith(DPAPI_KEY_PREFIX))
        else:
            self.assertEqual(reopened.key_path.stat().st_mode & 0o777, 0o600)

    def test_legacy_hash_only_password_becomes_viewable_after_login(self):
        target = {"id": self.store.state(self.alice)["user"]["id"]}
        with self.store.transaction(write=True) as db:
            db.execute("UPDATE accounts SET password_ciphertext=NULL WHERE id=?", (target["id"],))
        with self.assertRaises(Error) as legacy:
            self.store.view_password(self.admin, target)
        self.assertEqual(legacy.exception.status, 409)
        self.login("alice")
        self.assertEqual(self.store.view_password(self.admin, target), {"password": PASSWORD})

    def test_administrators_can_reset_each_other_without_old_password(self):
        self.store.bootstrap("admin2", "第二管理员", PASSWORD)
        second, second_state = self.login("admin2")
        first_id = self.store.state(self.admin)["user"]["id"]
        new_first = "new-first-admin-pass"
        self.store.mutate(second, "reset-password", {"id": first_id, "password": new_first})
        with self.assertRaises(Error):
            self.store.state(self.admin)
        self.admin, _ = self.store.login({"username": "admin", "password": new_first})
        result = self.store.mutate(self.admin, "reset-password", {"id": second_state["user"]["id"], "generate": True})
        with self.assertRaises(Error):
            self.store.state(second)
        _, state = self.store.login({"username": "admin2", "password": result["generatedPassword"]})
        self.assertEqual(state["user"]["role"], "admin")

    def test_operator_names_and_legacy_lock_migration(self):
        self.book(self.alice, 4, 6)
        self.assertEqual(self.store.state(self.bob)["records"][0]["operator"], "alice")
        self.admin_action("lock", **self.payload(8, 14, reason="维护"))
        self.admin_action("unlock", **self.payload(10, 12))
        locks = self.store.state(self.bob)["records"][1:]
        self.assertEqual([(r["s"], r["e"], r["operator"]) for r in locks], [(8, 10, "管理员"), (12, 14, "管理员")])

        legacy_path = self.path.with_name("legacy.sqlite3")
        with closing(sqlite3.connect(legacy_path)) as db, db:
            legacy_schema = SCHEMA.replace("    operator_id INTEGER REFERENCES accounts(id),\n", "")
            legacy_schema = legacy_schema.replace("    password_ciphertext BLOB,\n", "")
            legacy_schema = legacy_schema.replace(
                ",\n    status TEXT NOT NULL DEFAULT 'approved' CHECK(status IN ('pending','approved','rejected')),\n    registered_at INTEGER",
                "",
            )
            db.executescript(legacy_schema)
            db.execute("INSERT INTO accounts(username,name,password,role) VALUES (?,?,?,'user')",
                       ("legacy-user", "旧用户", encode_password(PASSWORD)))
            db.execute("INSERT INTO machines(id,name,ip,spec) VALUES (1,'legacy','192.0.2.123','')")
            db.execute("INSERT INTO intervals(machine_id,start,end,kind) VALUES (1,?,?,'locked')",
                       (self.now, self.now + SLOT))
        migrated = Store(legacy_path, clock=lambda: self.now)
        migrated.initialize()
        migrated.bootstrap("legacy-admin", "旧管理员", PASSWORD)
        _, state = migrated.login({"username": "legacy-admin", "password": PASSWORD})
        self.assertEqual(len(state["records"]), 1)
        self.assertIsNone(state["records"][0]["operator"])
        _, legacy_user = migrated.login({"username": "legacy-user", "password": PASSWORD})
        self.assertEqual(legacy_user["user"]["role"], "user")

    def test_registration_waits_for_any_admin_approval(self):
        result = self.store.register({"username": "e000001", "generate": True, "role": "admin"})
        password = result["generatedPassword"]
        self.assertRegex(password, r"^[A-Za-z0-9]{8}$")
        self.assertIn("等待审核", result["message"])
        self.assertIn("zhaoming / leixuewei", result["message"])
        credentials = {"username": "e000001", "password": password}
        with self.assertRaises(Error) as pending:
            self.store.login(credentials)
        self.assertEqual(pending.exception.code, "registration_pending")
        with self.assertRaises(Error) as wrong:
            self.store.login({**credentials, "password": "wrong-password"})
        self.assertEqual(wrong.exception.status, 401)
        state = self.store.state(self.admin)
        self.assertNotIn("e000001", {a["username"] for a in state["accounts"]})
        registration, = state["registrations"]
        self.assertNotIn("password", registration)
        self.assertEqual(self.store.state(self.alice)["registrations"], [])
        selection = {"decision": "approve", "ids": [registration["id"]]}
        with self.assertRaises(Error) as forbidden:
            self.store.mutate(self.alice, "review-registrations", selection)
        self.assertEqual(forbidden.exception.status, 403)
        second, _ = self.store.login({"username": "leixuewei", "password": "leixuewei"})
        self.store.mutate(second, "review-registrations", selection)
        _, approved = self.store.login(credentials)
        self.assertEqual(approved["user"]["role"], "user")
        self.assertEqual(approved["user"]["name"], "e000001")
        self.assertEqual(approved["machines"], [])
        self.assertEqual(self.store.view_password(second, {"id": registration["id"]}), {"password": password})

    def test_bulk_review_is_atomic_and_rejection_blocks_login(self):
        for n in range(4):
            self.store.register({"username": f"employee-{n}", "name": f"申请人{n}", "password": PASSWORD})
        selections = [a["id"] for a in self.store.state(self.admin)["registrations"]]
        approved = self.admin_action("review-registrations", decision="approve", ids=selections[:2])
        self.assertEqual(len(approved["registrations"]), 2)
        with self.assertRaises(Error) as stale:
            self.admin_action("review-registrations", decision="reject", ids=selections[1:])
        self.assertEqual(stale.exception.code, "state_changed")
        self.assertEqual([a["id"] for a in self.store.state(self.admin)["registrations"]], selections[2:])
        rejected = self.admin_action("review-registrations", decision="reject", ids=selections[2:])
        self.assertEqual(rejected["registrations"], [])
        self.login("employee-1")
        with self.assertRaises(Error) as denied:
            self.login("employee-2")
        self.assertEqual(denied.exception.code, "registration_rejected")
        self.assertIn("请联系管理员", str(denied.exception))

    def test_concurrent_review_preserves_first_decision(self):
        self.store.register({"username": "same-review", "password": PASSWORD})
        target, = self.store.state(self.admin)["registrations"]
        second, _ = self.store.login({"username": "zhaoming", "password": "zhaoming"})
        barrier = Barrier(2)

        def review(item):
            token, decision = item
            barrier.wait(timeout=5)
            try:
                self.store.mutate(token, "review-registrations", {"decision": decision, "ids": [target["id"]]})
                return decision
            except Error as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(review, [(self.admin, "approve"), (second, "reject")]))
        self.assertEqual(outcomes.count("state_changed"), 1)
        if "approve" in outcomes:
            self.login("same-review")
        else:
            with self.assertRaises(Error) as rejected:
                self.login("same-review")
            self.assertEqual(rejected.exception.code, "registration_rejected")

    def test_database_enforces_nonoverlap(self):
        self.book(self.alice, 2, 4)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "interval_overlap"):
            with self.store.transaction(write=True) as db:
                self.store.insert_interval(db, self.m, self.now + SLOT * 3, self.now + SLOT * 5, "locked")


class HTTPTests(Fixture):
    def test_http_conflict_and_cookie(self):
        try:
            server = Server(("127.0.0.1", 0), self.store)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EPERM):
                self.skipTest("本地网络监听受限")
            raise
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)

        def request(path, data=None, cookie=None, marker=True):
            with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=10)) as conn:
                headers = {"Content-Type": "application/json"}
                if marker:
                    headers["X-NPU-Request"] = "1"
                if cookie:
                    headers["Cookie"] = cookie
                conn.request("GET" if data is None else "POST", path, json.dumps(data) if data is not None else None, headers)
                response = conn.getresponse()
                if path.startswith("/api/"):
                    self.assertEqual(response.getheader("Cache-Control"), "no-store")
                return response.status, response.getheader("Set-Cookie"), response.read()

        status, _, html = request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"/app.js", html)
        self.assertEqual(request("/api/state")[0], 401)
        self.assertEqual(request("/api/login", {"username": "alice", "password": PASSWORD}, marker=False)[0], 403)
        cookies = []
        for username in ("alice", "bob"):
            status, cookie, body = request("/api/login", {"username": username, "password": PASSWORD})
            self.assertEqual(status, 200)
            self.assertIn("HttpOnly; SameSite=Strict", cookie)
            self.assertNotIn("password", json.loads(body)["user"])
            cookies.append(cookie.split(";", 1)[0])
        barrier = Barrier(2)
        data = self.payload(6, 10)

        def book(cookie):
            barrier.wait(timeout=5)
            status, _, body = request("/api/book", data, cookie)
            return status, json.loads(body)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = sorted(pool.map(book, cookies), key=lambda item: item[0])
        self.assertEqual([r[0] for r in responses], [200, 409])
        self.assertEqual(responses[1][1]["error"], "occupied")
        self.assertEqual(len(json.loads(request("/api/state", cookie=cookies[1])[2])["records"]), 1)
        target = {"id": self.store.state(self.alice)["user"]["id"]}
        self.assertEqual(request("/api/view-password", target)[0], 401)
        self.assertEqual(request("/api/view-password", target, cookies[0])[0], 403)
        _, cookie, _ = request("/api/login", {"username": "admin", "password": PASSWORD})
        status, _, body = request("/api/view-password", target, cookie.split(";", 1)[0])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"password": PASSWORD})
        self.assertEqual(request("/api/logout", {}, cookies[0])[0], 200)
        self.assertEqual(request("/api/state", cookie=cookies[0])[0], 401)
        credentials = {"username": "e-http", "password": PASSWORD}
        self.assertEqual(request("/api/register", credentials, marker=False)[0], 403)
        status, registration_cookie, _ = request("/api/register", credentials)
        self.assertEqual(status, 201)
        self.assertIsNone(registration_cookie)
        status, pending_cookie, body = request("/api/login", credentials)
        self.assertEqual(status, 403)
        self.assertIsNone(pending_cookie)
        self.assertEqual(json.loads(body)["error"], "registration_pending")
        admin_cookie = cookie.split(";", 1)[0]
        queue = json.loads(request("/api/state", cookie=admin_cookie)[2])["registrations"]
        self.assertEqual(request("/api/review-registrations", {"decision": "approve", "ids": [queue[0]["id"]]}, admin_cookie)[0], 200)
        self.assertEqual(request("/api/login", credentials)[0], 200)
