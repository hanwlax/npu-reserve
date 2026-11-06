#!/usr/bin/env python3
"""HTTP entry point and administrator bootstrap for NPU Reserve."""

import argparse
import getpass
import json
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict, deque
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock
from urllib.parse import urlsplit

from reservation import Error, PASSWORD_ACTIONS, SESSION_SECONDS, Store

STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "npu_session"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, store, secure_cookie=False):
        self.store = store
        self.secure_cookie = secure_cookie
        self.login_attempts = defaultdict(deque)
        self.login_lock = Lock()
        self.workers = BoundedSemaphore(32)
        self.password_workers = BoundedSemaphore(4)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()

    def limit_login(self, peer):
        now = time.monotonic()
        with self.login_lock:
            for address, attempts in list(self.login_attempts.items()):
                while attempts and now - attempts[0] >= 60:
                    attempts.popleft()
                if not attempts:
                    del self.login_attempts[address]
            attempts = self.login_attempts[peer]
            if len(attempts) >= 60:
                raise Error(429, "登录尝试过多，请稍后再试。")
            attempts.append(now)


class Handler(BaseHTTPRequestHandler):
    timeout = 15

    def send_bytes(self, status, content, mime, cookie=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(content)

    def json_response(self, status, data, cookie=None):
        self.send_bytes(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", cookie)

    def session(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        return cookies[COOKIE].value if COOKIE in cookies else None

    def cookie_header(self, token, max_age):
        secure = "; Secure" if self.server.secure_cookie else ""
        return f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}"

    def body(self):
        # A cross-origin browser request needs a CORS preflight to send this header.
        if self.headers.get("X-NPU-Request") != "1":
            raise Error(403, "请求来源校验失败。")
        if self.headers.get_content_type() != "application/json":
            raise Error(415, "请求必须使用 JSON。")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Error(400, "无效的请求长度。") from None
        if not 0 < size <= 65536:
            raise Error(413, "请求体长度超限或为空。")
        try:
            data = json.loads(self.rfile.read(size))
        except (ValueError, RecursionError):
            raise Error(400, "无效的 JSON。") from None
        if not isinstance(data, dict):
            raise Error(400, "请求必须是 JSON 对象。")
        return data

    def handle_error(self, error):
        if isinstance(error, Error):
            self.json_response(error.status, {"error": error.code, "message": str(error)})
        elif isinstance(error, sqlite3.OperationalError) and (
            str(error).startswith(("database is locked", "database table is locked", "database schema is locked"))
        ):
            self.json_response(503, {"error": "busy", "message": "服务暂时繁忙，请稍后重试。"})
        elif isinstance(error, sqlite3.IntegrityError) and str(error) == "interval_overlap":
            self.json_response(409, {"error": "occupied", "message": "时段已被占用，请重新选择。"})
        else:
            logging.exception("Request failed")
            self.json_response(500, {"error": "internal", "message": "服务器处理失败。"})

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == "/api/state":
                self.json_response(200, self.server.store.state(self.session()))
            elif path in ASSETS:
                name, mime = ASSETS[path]
                self.send_bytes(200, (STATIC / name).read_bytes(), mime)
            else:
                raise Error(404, "页面不存在。")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.handle_error(error)

    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            if not path.startswith("/api/"):
                raise Error(404, "接口不存在。")
            action = path[5:]
            data = self.body()
            token = self.session()
            store = self.server.store
            if action == "login":
                self.server.limit_login(self.client_address[0])
                with self.server.password_workers:
                    session, state = store.login(data, token)
                self.json_response(200, state, self.cookie_header(session, SESSION_SECONDS))
            elif action == "logout":
                store.logout(token)
                self.json_response(200, {}, self.cookie_header("", 0))
            elif action == "view-password":
                self.json_response(200, store.view_password(token, data))
            else:
                if action in PASSWORD_ACTIONS:
                    with self.server.password_workers:
                        result = store.mutate(token, action, data)
                else:
                    result = store.mutate(token, action, data)
                self.json_response(200, result)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.handle_error(error)


def main():
    parser = argparse.ArgumentParser(description="NPU 机时预约")
    parser.add_argument("--db", default=os.environ.get("NPU_RESERVE_DB", "data/reservations.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动 HTTP 服务")
    serve.add_argument("--host", default=os.environ.get("NPU_RESERVE_HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=os.environ.get("NPU_RESERVE_PORT", "8080"))
    serve.add_argument("--secure-cookie", action="store_true", default=os.environ.get("NPU_RESERVE_SECURE_COOKIE") == "1")
    admin = commands.add_parser("create-admin", help="交互式创建管理员账户")
    admin.add_argument("--username", required=True)
    admin.add_argument("--name", default="管理员")
    args = parser.parse_args()
    os.umask(0o077)
    store = Store(args.db)
    store.initialize()
    if args.command == "create-admin":
        try:
            password = getpass.getpass("初始密码（至少 8 个字符）: ")
            if password != getpass.getpass("确认密码: "):
                raise Error(422, "两次密码不一致。")
            store.bootstrap(args.username, args.name, password)
        except Error as error:
            parser.exit(1, f"{error}\n")
        print(f"已创建管理员 {args.username}")
        return
    with Server((args.host, args.port), store, args.secure_cookie) as httpd:
        print(f"NPU 机时预约正在监听 {args.host}:{httpd.server_port}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
