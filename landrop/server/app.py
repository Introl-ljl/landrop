#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LAN Drop —— 局域网文件收集与快捷分享（单文件服务器 + 持久状态层）。

核心模型
--------
* **空间（space）**：一个独立的共享目录 + 独立的链接集合。
* **授权（grant）**：一条链接或一个口令。分 ``admin``（管理）与 ``share``（分享）
  两类，各有独立权限档：``upload`` / ``delete_own`` / ``full``。
* **访客（visitor）**：浏览器匿名主体。secret 只放 HttpOnly Cookie，库中仅存摘要。
* **会话（session）**：短期登录凭证，绑定 grant 版本；撤销/轮换立即失效。

权限判定全部在服务端完成，客户端提交的文件名、owner 等字段一律不采信。

运行::

    landrop serve --data-dir ./data
    landrop serve --data-dir ./data --dir /srv/share     # 指定共享目录
    landrop serve --data-dir ./data --import ./uploads   # 导入历史目录
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sys
import socketserver
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from landrop import __version__ as VERSION
from landrop.common import resolve_data_dir, setup_console

from . import store as store_mod
from .store import Store

_HERE = os.path.dirname(os.path.abspath(__file__))

APP_NAME = "LAN Drop"
CHUNK_SIZE = 1024 * 1024
MAX_KEY_ATTEMPTS = 10
ATTEMPT_WINDOW = 60

COOKIE_SESSION = "ld_session"
COOKIE_VISITOR = "ld_visitor"

CAPS = {
    "upload": {"list": False, "download": False, "upload": True, "delete": False},
    "read": {"list": True, "download": True, "upload": False, "delete": False},
    "delete_own": {"list": True, "download": True, "upload": True, "delete": "own"},
    "full": {"list": True, "download": True, "upload": True, "delete": "all"},
    "admin": {"list": True, "download": True, "upload": True, "delete": "all",
              "manage": True},
}

STORE: Store | None = None          # 由 main() 设置
DEFAULT_SPACE: str = ""             # 默认空间 id
PUBLIC_URL: str = ""                # 供管理面板拼接分享链接
_attempts: dict = {}
_attempts_lock = threading.Lock()
_upload_locks: dict = {}
_upload_locks_lock = threading.Lock()
TOKEN_PATH = re.compile(r"/([A-Za-z0-9_-]{16,64})/?")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


_static_cache: str | None = None


def resource_dir() -> str:
    """静态资源目录；兼容 PyInstaller 解包目录，以及 zipapp（.pyz）里不能按路径读文件的情况。"""
    global _static_cache
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cand = os.path.join(base, "landrop", "server", "static")
        if os.path.isdir(cand):
            return cand
    plain = os.path.join(_HERE, "static")
    if os.path.isdir(plain):
        return plain
    if _static_cache is None:                      # 在 .pyz 里：把 static 解到临时目录一次
        import tempfile
        import zipfile
        archive = _HERE
        while archive and not os.path.isfile(archive):
            parent = os.path.dirname(archive)
            if parent == archive:
                break
            archive = parent
        dest = tempfile.mkdtemp(prefix="landrop-static-")
        prefix = os.path.relpath(_HERE, archive).replace(os.sep, "/") + "/static/"
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if name.startswith(prefix) and not name.endswith("/"):
                    target = os.path.join(dest, name[len(prefix):])
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, "wb") as f:
                        f.write(zf.read(name))
        _static_cache = dest
    return _static_cache


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def human_size(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def get_upload_lock(upload_id: str) -> threading.Lock:
    with _upload_locks_lock:
        lock = _upload_locks.get(upload_id)
        if lock is None:
            lock = threading.Lock()
            _upload_locks[upload_id] = lock
        return lock


def drop_upload_lock(upload_id: str) -> None:
    with _upload_locks_lock:
        _upload_locks.pop(upload_id, None)


class Handler(BaseHTTPRequestHandler):
    server_version = f"Landrop/{VERSION}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础
    def _send_json(self, obj, status=HTTPStatus.OK, cookies=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, status, message, **extra):
        payload = {"ok": False, "error": message}
        payload.update(extra)
        self._send_json(payload, status=status)

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(min(CHUNK_SIZE, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    def _json_body(self, limit: int = 16384):
        try:
            cl = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            cl = 0
        if cl > limit:
            self._drain_body()
            return None
        raw = self._read_exact(cl)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None

    def _drain_body(self):
        remaining = 0
        cl = self.headers.get("Content-Length")
        if cl and cl.isdigit():
            remaining = int(cl)
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self.close_connection = True
            return
        while remaining > 0:
            chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _cookies(self) -> dict:
        out = {}
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                out[k.strip()] = v.strip()
        return out

    def _client_ip(self) -> str:
        return self.client_address[0]

    # ------------------------------------------------------------ 会话
    def _auth(self):
        """解析当前会话；返回 dict 或 None。"""
        token = self._cookies().get(COOKIE_SESSION, "")
        if not token:
            return None
        return STORE.session_from_token(token)

    def _caps(self, auth) -> dict:
        if auth is None:
            return {}
        if auth["grant"]["kind"] == "admin":
            caps = dict(CAPS["admin"])
            caps["spaces"] = True
            return caps
        caps = dict(CAPS[auth["perm"]])
        caps["spaces"] = False
        return caps

    def _rate_limited(self) -> bool:
        ip = self._client_ip()
        ts = time.time()
        with _attempts_lock:
            arr = [t for t in _attempts.get(ip, []) if ts - t < ATTEMPT_WINDOW]
            _attempts[ip] = arr
            return len(arr) >= MAX_KEY_ATTEMPTS

    def _record_attempt(self):
        ip = self._client_ip()
        ts = time.time()
        with _attempts_lock:
            arr = [t for t in _attempts.get(ip, []) if ts - t < ATTEMPT_WINDOW]
            arr.append(ts)
            _attempts[ip] = arr
            if len(_attempts) > 4096:       # 顺带回收不再活跃的 IP，避免表无限增长
                for k in [k for k, v in _attempts.items()
                          if not v or ts - v[-1] >= ATTEMPT_WINDOW]:
                    _attempts.pop(k, None)

    def _clear_attempts(self):
        with _attempts_lock:
            _attempts.pop(self._client_ip(), None)

    def _identity(self, auth):
        """为当前请求取得访客身份；没有 Cookie 时新建。返回 (visitor_row, set_cookie)。"""
        token = self._cookies().get(COOKIE_VISITOR, "")
        row = STORE.visitor_from_secret(token) if token else None
        if row is not None and row["id"] == auth["visitor"]["id"]:
            return row, None
        if row is None:
            vid, secret = STORE.create_visitor()
            row = STORE.visitor_from_secret(secret)
            cookie = (f"{COOKIE_VISITOR}={secret}; Path=/; HttpOnly; SameSite=Lax; "
                      f"Max-Age={store_mod.VISITOR_TTL}")
            return row, cookie
        # Cookie 与登录会话不一致：以会话记录为准
        return auth["visitor"], None

    def _cs_ok(self) -> bool:
        """轻量 CSRF 防护：带 Origin 的写请求必须同源。"""
        if self.command in ("GET", "HEAD"):
            return True
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        try:
            netloc = urlparse(origin).netloc
        except ValueError:
            return False
        return netloc == host

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
        except Exception:
            self._err(HTTPStatus.BAD_REQUEST, "bad url")
            return
        try:
            if path == "/api/login" and method == "POST":
                return self.api_login()
            if path == "/api/logout" and method == "POST":
                return self.api_logout()
            if path == "/api/me" and method in ("GET", "HEAD"):
                return self.api_me()
            if path == "/api/files" and method in ("GET", "HEAD"):
                return self.api_list(parsed)
            if path == "/api/upload" and method == "PUT":
                return self.api_upload(parsed)
            if path == "/api/upload/abort" and method == "DELETE":
                return self.api_upload_abort(parsed)
            if path == "/api/download" and method in ("GET", "HEAD"):
                return self.api_download(parsed)
            if path == "/api/delete" and method == "DELETE":
                return self.api_delete(parsed)
            if path == "/api/verify" and method in ("GET", "HEAD"):
                return self.api_verify(parsed)
            if path == "/api/grants" and method in ("GET", "HEAD"):
                return self.api_grants_list()
            if path == "/api/grants" and method == "POST":
                return self.api_grants_create()
            if path == "/api/grants/revoke" and method == "POST":
                return self.api_grants_revoke()
            if path == "/api/grants/rotate" and method == "POST":
                return self.api_grants_rotate()
            if path == "/api/transfers" and method in ("GET", "HEAD"):
                return self.api_transfers_list()
            if path == "/api/transfers" and method == "POST":
                return self.api_transfers_create()
            if path == "/api/done" and method == "POST":
                return self.api_done()
            if path == "/api/spaces" and method in ("GET", "HEAD"):
                return self.api_spaces_list()
            if path == "/api/spaces" and method == "POST":
                return self.api_spaces_create()
            if path in ("/admin", "/admin/"):
                return self.serve_static("admin.html")
            if path == "/" and method in ("GET", "HEAD"):
                return self.serve_static("index.html")
            if method in ("GET", "HEAD"):
                rel = path.lstrip("/").replace("\\", "/")
                if rel and ".." not in rel.split("/") and all(ord(c) >= 32 for c in rel):
                    if (TOKEN_PATH.fullmatch(path)
                            and not os.path.isfile(os.path.join(resource_dir(), rel))):
                        # 文件码 = 地址/令牌：浏览器直接打开也能取件（网页从路径读出令牌并自动登录）
                        return self.serve_static("index.html", private=True)
                    return self.serve_static(rel)
            self._drain_body()
            self._err(HTTPStatus.NOT_FOUND, "not found")
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except Exception as exc:  # noqa: BLE001
            log(f"!! {method} {self.path} 出错: {exc!r}")
            try:
                self._err(HTTPStatus.INTERNAL_SERVER_ERROR, "server error")
            except Exception:
                pass

    # ------------------------------------------------------------ 静态
    def serve_static(self, rel: str, private: bool = False):
        base = os.path.realpath(resource_dir())
        full = os.path.realpath(os.path.join(base, rel))
        if full != base and not full.startswith(base + os.sep):
            return self._err(HTTPStatus.NOT_FOUND, "not found")
        if not os.path.isfile(full):
            self._err(HTTPStatus.NOT_FOUND, "not found")
            return
        ctype, _ = mimetypes.guess_type(full)
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        size = os.path.getsize(full)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store" if private else "no-cache")
        if private:                     # 地址里带令牌：不让它经 Referer 泄露给页面外的请求
            self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            with open(full, "rb") as f:
                shutil.copyfileobj(f, self.wfile, CHUNK_SIZE)

    # ------------------------------------------------------------ 登录
    def api_login(self):
        if self._rate_limited():
            return self._err(HTTPStatus.TOO_MANY_REQUESTS, "尝试次数过多，请稍后再试")
        data = self._json_body(4096)
        if data is None:
            self._record_attempt()
            return self._err(HTTPStatus.BAD_REQUEST, "invalid json")
        if not self._cs_ok():
            return self._err(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")

        raw_key = str(data.get("secret") or data.get("key") or "").strip()
        grant = STORE.find_login_grant(raw_key) if raw_key else None
        ok, reason = (False, "密钥错误")
        if grant is not None:
            ok, reason = STORE.grant_state(grant["id"])

        if not ok:
            self._record_attempt()
            log(f"登录失败  <- {self._client_ip()}  ({reason})")
            time.sleep(0.4)
            return self._err(HTTPStatus.UNAUTHORIZED, reason if grant is not None else "密钥错误")

        self._clear_attempts()
        visitor, vcookie = self._identity_for_login(grant)
        token = STORE.create_session(visitor["id"], grant["id"], grant["version"])
        STORE.touch_grant(grant["id"])
        cookies = [
            f"{COOKIE_SESSION}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={store_mod.SESSION_TTL}"
        ]
        if vcookie:
            cookies.append(vcookie)
        log(f"登录成功  <- {self._client_ip()}  {grant['kind']}/{grant['perm']}"
            f"{'（管理密钥）' if grant['kind'] == 'admin' else ''}")
        self._send_json(
            {
                "ok": True,
                "kind": grant["kind"],
                "range_progress": True,
                "perm": grant["perm"],
                "label": grant["label"],
                "capabilities": CAPS["admin"] if grant["kind"] == "admin" else CAPS[grant["perm"]],
            },
            cookies=cookies,
        )

    def _identity_for_login(self, grant):
        """登录时确定访客身份：复用浏览器 Cookie，否则新建。"""
        token = self._cookies().get(COOKIE_VISITOR, "")
        row = STORE.visitor_from_secret(token) if token else None
        if row is not None:
            STORE.touch_visitor(row["id"])
            return row, None
        vid, secret = STORE.create_visitor()
        row = STORE.visitor_from_secret(secret)
        cookie = (f"{COOKIE_VISITOR}={secret}; Path=/; HttpOnly; SameSite=Lax; "
                  f"Max-Age={store_mod.VISITOR_TTL}")
        return row, cookie

    def api_logout(self):
        token = self._cookies().get(COOKIE_SESSION, "")
        if token:
            STORE.delete_session(token)
        cookie = f"{COOKIE_SESSION}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        self._send_json({"ok": True}, cookies=[cookie])

    def api_me(self):
        auth = self._auth()
        if auth is None:
            return self._send_json({"ok": True, "logged_in": False})
        g = auth["grant"]
        space = STORE.get_space(g["space_id"]) if g["space_id"] else None
        self._send_json({
            "ok": True,
            "logged_in": True,
            "kind": g["kind"],
            "perm": g["perm"],
            "label": g["label"],
            "space": {"id": space["id"], "name": space["name"]} if space else None,
            "capabilities": self._caps(auth),
            "server_time": time.time(),
        })

    def _space_for(self, auth, parsed=None, data=None, default=None):
        """确定本次请求作用于哪个空间。管理员必须显式指定；其他人固定为自己的空间。"""
        g = auth["grant"]
        if g["kind"] != "admin":
            return g["space_id"]
        sid = None
        if parsed is not None:
            sid = (parse_qs(parsed.query).get("space") or [""])[0]
        if not sid and isinstance(data, dict):
            sid = str(data.get("space_id") or data.get("space") or "")
        if not sid:
            sid = default or DEFAULT_SPACE
        return sid if sid and STORE.get_space(sid) else None

    # ------------------------------------------------------------ 列表
    def api_list(self, parsed=None):
        auth = self._auth()
        if auth is None:
            return self._err(HTTPStatus.UNAUTHORIZED, "未登录")
        caps = self._caps(auth)
        space_id = self._space_for(auth, parsed)
        if space_id is None:
            return self._err(HTTPStatus.BAD_REQUEST, "空间不存在")
        if not caps["list"]:
            # 仅上传档：不暴露文件列表，但仍告知空间名称
            space = STORE.get_space(space_id)
            return self._send_json({
                "ok": True, "files": [],
                "space": {"id": space_id, "name": space["name"] if space else ""},
                "server_time": time.time(), "capabilities": caps, "owner_filtered": False,
                "note": "当前链接仅允许上传：文件会进入这个共享空间，但本链接不展示已有文件。",
            })
        rows = STORE.list_files(space_id)
        space = STORE.get_space(space_id)
        my_visitor = auth["visitor"]["id"]
        fetched = (STORE.fetched_files(auth["grant"]["id"], my_visitor)
                   if self._transfer_grant(auth) is not None else set())
        delete_scope = caps.get("delete")
        self._send_json({
            "ok": True,
            "space": {"id": space_id, "name": space["name"] if space else ""},
            "owner_filtered": False,
            "capabilities": caps,
            "server_time": time.time(),
            "files": [
                {
                    "id": r["id"],
                    "received": r["id"] in fetched,
                    "name": r["display_name"],
                    "stored_name": r["stored_name"],
                    "size": r["size"],
                    "mtime": r["created_at"],
                    "sha256": r["sha256"],
                    "mine": r["owner_visitor"] == my_visitor,
                    "origin": r["origin"],
                    "deletable": bool(
                        delete_scope == "all"
                        or (delete_scope == "own"
                            and r["owner_visitor"] == my_visitor
                            and r["owner_grant"] == auth["grant"]["id"])
                    ),
                }
                for r in rows
            ],
        })

    # ------------------------------------------------------------ 上传
    def api_upload(self, parsed):
        auth = self._auth()
        if auth is None:
            self._drain_body()
            return self._err(HTTPStatus.UNAUTHORIZED, "未登录")
        if not self._cs_ok():
            self._drain_body()
            return self._err(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
        caps = self._caps(auth)
        if not caps["upload"]:
            self._drain_body()
            return self._err(HTTPStatus.FORBIDDEN, "当前链接没有上传权限")

        qs = parse_qs(parsed.query)
        raw_name = (qs.get("name") or [""])[0]
        upload_id = (qs.get("id") or [""])[0] or secrets.token_hex(8)
        try:
            offset = int((qs.get("offset") or ["0"])[0] or 0)
            total_raw = (qs.get("total") or [None])[0]
            total = int(total_raw) if total_raw and total_raw.isdigit() else None
        except ValueError:
            self._drain_body()
            return self._err(HTTPStatus.BAD_REQUEST, "参数错误")
        expect = str((qs.get("sha256") or [""])[0] or "").strip().lower()
        if expect and not re.fullmatch(r"[0-9a-f]{64}", expect):
            self._drain_body()
            return self._err(HTTPStatus.BAD_REQUEST, "sha256 参数格式错误")
        if offset < 0 or (total is not None and (total < 0 or offset > total)):
            self._drain_body()
            return self._err(HTTPStatus.BAD_REQUEST, "offset/total 不合法")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            self._drain_body()
            return self._err(HTTPStatus.BAD_REQUEST, "Content-Length 不合法")

        name = store_mod.safe_stored_name(raw_name)
        raw_path = (qs.get("path") or [""])[0]
        if raw_path:                    # 文件夹上传：显示名带相对路径，接收端据此还原目录
            try:
                name = store_mod.safe_rel_name(raw_path)
            except ValueError:
                self._drain_body()
                return self._err(HTTPStatus.BAD_REQUEST, "path 不合法")
        space_id = self._space_for(auth, parsed)
        if space_id is None:
            self._drain_body()
            return self._err(HTTPStatus.BAD_REQUEST, "空间不存在")
        visitor = auth["visitor"]["id"]
        grant_id = auth["grant"]["id"]
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", upload_id)[:64] or secrets.token_hex(8)

        lock = get_upload_lock(safe_id)
        with lock:
            completed = STORE.get_upload_receipt(safe_id)
            if completed is not None:
                self._drain_body()
                if (completed["visitor_id"] != visitor or completed["grant_id"] != grant_id
                        or completed["space_id"] != space_id):
                    return self._err(HTTPStatus.FORBIDDEN, "该上传会话不属于当前身份")
                receipt = json.loads(completed["receipt"])
                if (receipt["name"] != name or (total is not None and receipt["size"] != total)
                        or (expect and receipt["sha256"] != expect)):
                    return self._err(HTTPStatus.CONFLICT, "上传 id 已用于其他文件")
                return self._send_json(receipt)
            row = STORE.get_upload(safe_id)
            if row is None:
                if offset != 0:
                    self._drain_body()
                    return self._err(HTTPStatus.CONFLICT, "上传会话不存在或已过期", expected_offset=0)
                part = STORE.part_path(safe_id)
                if os.path.exists(part):
                    os.remove(part)
                STORE.create_upload(safe_id, space_id, visitor, grant_id, name, total)
                row = STORE.get_upload(safe_id)
            else:
                same_owner = (row["visitor_id"] == visitor and row["grant_id"] == grant_id
                              and row["space_id"] == space_id)
                if not same_owner:
                    self._drain_body()
                    self._err(HTTPStatus.FORBIDDEN, "该上传会话不属于当前身份")
                    return
                if name != row["display_name"]:
                    name = row["display_name"]

            part = STORE.part_path(safe_id)
            existing = os.path.getsize(part) if os.path.exists(part) else 0
            if offset != existing:
                self._drain_body()
                self.send_response(HTTPStatus.CONFLICT)
                self.send_header("X-Expected-Offset", str(existing))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            remaining = length
            with open(part, "ab" if offset > 0 else "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                if remaining == 0 and total is not None and existing + length >= total:
                    f.flush()
                    os.fsync(f.fileno())

            written = length - remaining
            if remaining > 0:
                STORE.update_upload(safe_id, existing + written)
                return self._send_json({
                    "ok": False, "error": "incomplete", "id": safe_id,
                    "received": existing + written,
                }, status=HTTPStatus.BAD_REQUEST)

            new_size = existing + written
            if total is not None and new_size < total:
                STORE.update_upload(safe_id, new_size)
                return self._send_json({
                    "ok": True, "id": safe_id, "received": new_size, "complete": False,
                })
            if total is not None and new_size > total:
                self._cleanup_upload(safe_id)
                return self._err(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "接收字节数超过声明大小")

            return self._finish_upload(safe_id, part, new_size, expect, name)

    def _finish_upload(self, upload_id, part, size, expect, name):
        """分块齐全后：校验摘要 → 原子发布 → 登记索引并返回回执。"""
        try:
            actual = store_mod.file_sha256(part)
        except OSError as exc:
            self._cleanup_upload(upload_id)
            return self._err(HTTPStatus.INTERNAL_SERVER_ERROR, f"读取分块失败: {exc}")

        if expect and actual != expect:
            self._cleanup_upload(upload_id)
            log(f"校验失败  <- {self._client_ip()}  {name}  期望 {expect[:12]}… 实际 {actual[:12]}…")
            return self._send_json({
                "ok": False,
                "error": "sha256 校验失败，文件未保存",
                "expected": expect,
                "actual": actual,
            }, status=HTTPStatus.UNPROCESSABLE_ENTITY)

        auth_space = STORE.get_upload(upload_id)["space_id"]
        stored = STORE.unique_stored_name(auth_space, store_mod.safe_stored_name(name))
        final = os.path.join(STORE.space_dir(auth_space), stored)
        try:
            os.replace(part, final)
        except OSError as exc:
            self._cleanup_upload(upload_id)
            return self._err(HTTPStatus.INTERNAL_SERVER_ERROR, f"落盘失败: {exc}")

        row = STORE.get_upload(upload_id)
        fid = STORE.add_file(
            space_id=auth_space,
            stored_name=stored,
            display_name=name,
            size=size,
            sha256=actual,
            origin="upload",
            owner_visitor=row["visitor_id"],
            owner_grant=row["grant_id"],
        )
        receipt = {
            "ok": True,
            "complete": True,
            "id": fid,
            "name": name,
            "stored_name": stored,
            "size": size,
            "sha256": actual,
            "verified": bool(expect),
        }
        STORE.save_upload_receipt(row, receipt)
        STORE.drop_upload(upload_id)
        drop_upload_lock(upload_id)
        log(f"上传完成  <- {self._client_ip()}  {stored}  ({human_size(size)})  sha256={actual[:12]}…")
        self._send_json(receipt)

    def _cleanup_upload(self, upload_id):
        try:
            os.remove(STORE.part_path(upload_id))
        except FileNotFoundError:
            pass
        except OSError:
            pass
        STORE.drop_upload(upload_id)
        drop_upload_lock(upload_id)

    def api_upload_abort(self, parsed):
        auth = self._auth()
        if auth is None:
            return self._err(HTTPStatus.UNAUTHORIZED, "未登录")
        qs = parse_qs(parsed.query)
        upload_id = re.sub(r"[^A-Za-z0-9_-]", "", (qs.get("id") or [""])[0])[:64]
        if not upload_id:
            return self._err(HTTPStatus.BAD_REQUEST, "missing id")
        row = STORE.get_upload(upload_id)
        if row is None:
            return self._send_json({"ok": True, "note": "上传会话已清理"})
        if not (row["visitor_id"] == auth["visitor"]["id"]
                and row["grant_id"] == auth["grant"]["id"]
                and row["space_id"] == self._space_for(auth, parsed)):
            return self._err(HTTPStatus.FORBIDDEN, "该上传会话不属于当前身份")
        self._cleanup_upload(upload_id)
        return self._send_json({"ok": True})

    # ------------------------------------------------------------ 下载
    def _lookup_file(self, auth, file_id: str, parsed=None, require_download=True):
        """按不可变 id 定位文件，并校验它属于当前空间与可见范围。"""
        if not file_id:
            return None, "缺少文件 id"
        row = STORE.get_file(file_id)
        if row is None or row["status"] != "active":
            return None, "文件不存在或已删除"
        space_id = self._space_for(auth, parsed)
        if space_id is None:
            return None, "空间不存在"
        if auth["grant"]["kind"] != "admin" and row["space_id"] != space_id:
            return None, "文件不在当前空间"
        caps = self._caps(auth)
        if require_download and not caps.get("download"):
            return None, "没有下载权限"
        return row, ""

    def api_download(self, parsed):
        auth = self._auth()
        if auth is None:
            self._drain_body()
            return self._err(HTTPStatus.UNAUTHORIZED, "未登录")
        qs = parse_qs(parsed.query)
        file_id = (qs.get("id") or [""])[0]
        legacy = (qs.get("name") or [""])[0]
        if not file_id and legacy:
            space_id = self._space_for(auth, parsed)
            found = ([r for r in STORE.list_files(space_id) if r["display_name"] == legacy]
                     if space_id else [])
            if len(found) == 1:
                file_id = found[0]["id"]
        row, why = self._lookup_file(auth, file_id, parsed)
        if row is None:
            return self._err(HTTPStatus.NOT_FOUND if "不存在" in why else HTTPStatus.FORBIDDEN, why)

        if qs.get("progress") == ["1"]:
            ranges = (STORE.get_fetch_ranges(auth["grant"]["id"], auth["visitor"]["id"], row["id"])
                      if self._transfer_grant(auth) is not None else [[0, row["size"]]])
            return self._send_json({"ok": True, "ranges": ranges})

        full = STORE.file_path(row)
        if not os.path.isfile(full):
            return self._err(HTTPStatus.NOT_FOUND, "文件已不在磁盘上")
        name = row["stored_name"]
        size = os.path.getsize(full)
        ctype, _ = mimetypes.guess_type(name)
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
            "application/json", "application/javascript", "application/xml",
        ):
            ctype += "; charset=utf-8"

        start, end = 0, max(size - 1, 0)
        partial = False
        range_hdr = self.headers.get("Range")
        if size and range_hdr and range_hdr.startswith("bytes="):
            spec = range_hdr[6:].split(",")[0].strip()
            s, _, e = spec.partition("-")
            try:
                if s == "" and e:
                    start = max(0, size - int(e))
                else:
                    start = int(s)
                    if e:
                        end = min(int(e), size - 1)
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True
            except ValueError:
                partial = False
                start, end = 0, max(size - 1, 0)

        length = end - start + 1 if size else 0
        status = HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK
        disposition = "inline" if self._inline_ok(ctype) else "attachment"

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition",
                         f"{disposition}; "
                         f"{self._content_disposition(row['display_name'].rsplit('/', 1)[-1])}")
        self.send_header("Accept-Ranges", "bytes")
        if row["sha256"]:
            self.send_header("X-File-SHA256", row["sha256"])
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        left = length
        try:
            if size:
                with open(full, "rb") as f:
                    f.seek(start)
                    while left > 0:
                        chunk = f.read(min(CHUNK_SIZE, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
        finally:
            # Successful writes still count when a later write loses the connection.
            if self._transfer_grant(auth) is not None:
                if STORE.record_fetch_range(auth["grant"]["id"], auth["visitor"]["id"], row["id"],
                                            start, start + length - left):
                    log(f"传输取件次数已用完  <- {self._client_ip()}  {auth['grant']['id']}")

    @staticmethod
    def _inline_ok(ctype: str) -> bool:
        return (
            ctype.startswith("image/")
            or ctype.startswith("video/")
            or ctype.startswith("audio/")
            or ctype.startswith("text/plain")
            or ctype in ("application/pdf", "application/json")
        )

    @staticmethod
    def _content_disposition(name: str) -> str:
        ascii_name = name.encode("ascii", "ignore").decode("ascii") or "download"
        ascii_name = ascii_name.replace('"', "'").replace("\\", "_")
        return f"filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"

    # ------------------------------------------------------------ 校验
    def api_verify(self, parsed):
        """重新计算当前文件的 SHA-256，用于确认"现在磁盘上的内容"是否仍与登记一致。"""
        auth = self._auth()
        if auth is None:
            return self._err(HTTPStatus.UNAUTHORIZED, "未登录")
        qs = parse_qs(parsed.query)
        row, why = self._lookup_file(auth, (qs.get("id") or [""])[0])
        if row is None:
            return self._err(HTTPStatus.FORBIDDEN if "权限" in why else HTTPStatus.NOT_FOUND, why)
        full = STORE.file_path(row)
        if not os.path.isfile(full):
            return self._err(HTTPStatus.NOT_FOUND, "文件已不在磁盘上")
        actual = store_mod.file_sha256(full)
        recorded = row["sha256"] or ""
        match = bool(recorded) and secrets.compare_digest(actual, recorded)
        self._send_json({
            "ok": True,
            "id": row["id"],
            "name": row["display_name"],
            "size": os.path.getsize(full),
            "sha256": actual,
            "recorded": recorded,
            "recorded_missing": not recorded,
            "match": match,
            "checked_at": time.time(),
        })

    # ------------------------------------------------------------ 删除
    def api_delete(self, parsed):
        auth = self._auth()
        if auth is None:
            return self._err(HTTPStatus.UNAUTHORIZED, "未登录")
        if not self._cs_ok():
            return self._err(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
        caps = self._caps(auth)
        if not caps.get("delete"):
            return self._err(HTTPStatus.FORBIDDEN, "当前链接没有删除权限")
        qs = parse_qs(parsed.query)
        file_id = (qs.get("id") or [""])[0]
        row = STORE.get_file(file_id) if file_id else None
        if row is None or row["status"] != "active":
            return self._err(HTTPStatus.NOT_FOUND, "文件不存在或已删除")
        if auth["grant"]["kind"] != "admin" and row["space_id"] != self._space_for(auth, parsed):
            return self._err(HTTPStatus.FORBIDDEN, "文件不在当前空间")
        if caps["delete"] == "own":
            same_visitor = row["owner_visitor"] == auth["visitor"]["id"]
            same_grant = row["owner_grant"] == auth["grant"]["id"]
            if not (same_visitor and same_grant):
                return self._err(HTTPStatus.FORBIDDEN, "只能删除自己上传的文件")

        moved = STORE.move_to_trash(row)
        STORE.mark_deleted(row["id"], auth["grant"]["id"],
                           status="deleted" if moved else "missing")
        log(f"删除  <- {self._client_ip()}  {row['display_name']}"
            f"{'（含回收站副本）' if moved else '（文件已在磁盘上缺失）'}")
        self._send_json({"ok": True, "id": row["id"], "name": row["display_name"],
                         "recoverable": bool(moved)})

    # ------------------------------------------------------------ 管理
    def _require_admin(self):
        auth = self._auth()
        if auth is None:
            self._err(HTTPStatus.UNAUTHORIZED, "未登录")
            return None
        if auth["grant"]["kind"] != "admin":
            self._err(HTTPStatus.FORBIDDEN, "需要管理员密钥")
            return None
        return auth

    def _grant_public(self, row, secret=None):
        out = {
            "id": row["id"],
            "kind": row["kind"],
            "space_id": row["space_id"],
            "label": row["label"],
            "perm": row["perm"],
            "mode": row["mode"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "revoked": bool(row["revoked"]),
            "last_used_at": row["last_used_at"],
            "url": "",
        }
        if row["kind"] == "share" and row["mode"] == "token" and secret:
            out["url"] = f"{PUBLIC_URL}/#k={secret}"
        if secret:
            out["secret"] = secret
        return out

    def api_grants_list(self):
        auth = self._require_admin()
        if auth is None:
            return
        spaces = {s["id"]: s["name"] for s in STORE.list_spaces()}
        grants = []
        for row in STORE.list_grants():
            item = self._grant_public(row)
            item["space_name"] = spaces.get(row["space_id"], "")
            grants.append(item)
        self._send_json({
            "ok": True,
            "grants": grants,
            "spaces": [{"id": s["id"], "name": s["name"], "dir": s["dir"],
                        "path": STORE.space_dir(s["id"])} for s in STORE.list_spaces()],
            "data_dir": STORE.data_dir,
            "public_url": PUBLIC_URL,
            "perms": list(store_mod.VALID_PERMS),
        })

    def api_grants_create(self):
        auth = self._require_admin()
        if auth is None:
            return
        if not self._cs_ok():
            return self._err(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
        data = self._json_body()
        if data is None:
            return self._err(HTTPStatus.BAD_REQUEST, "invalid json")
        kind = str(data.get("kind") or "share")
        if kind not in ("share", "admin"):
            return self._err(HTTPStatus.BAD_REQUEST, "kind 不合法")
        mode = str(data.get("mode") or "token")
        if mode not in store_mod.VALID_MODES:
            return self._err(HTTPStatus.BAD_REQUEST, "mode 不合法")
        perm = str(data.get("perm") or "upload")
        if perm not in store_mod.VALID_PERMS:
            return self._err(HTTPStatus.BAD_REQUEST, "perm 不合法")
        label = str(data.get("label") or "")[:60]
        space_id = data.get("space_id") or None
        password = data.get("password") or None
        expires_days = data.get("expires_days")

        if kind == "admin":
            space_id = None
            perm = "full"
        else:
            if space_id is None:
                space_id = DEFAULT_SPACE
            if STORE.get_space(space_id) is None:
                return self._err(HTTPStatus.BAD_REQUEST, "空间不存在")
        expires_at = None
        try:
            days = float(expires_days) if expires_days not in (None, "", 0, "0") else 0
            if days > 0:
                expires_at = time.time() + days * 86400
        except (TypeError, ValueError):
            return self._err(HTTPStatus.BAD_REQUEST, "expires_days 不合法")

        row, secret = STORE.create_grant(
            kind=kind, space_id=space_id, perm=perm, mode=mode, label=label,
            password=str(password) if password else None, expires_at=expires_at,
        )
        log(f"新建授权  <- {self._client_ip()}  {row['id']}  {kind}/{perm}/{mode}")
        self._send_json({"ok": True, "grant": self._grant_public(row, secret)}, status=HTTPStatus.CREATED)

    def api_grants_revoke(self):
        auth = self._require_admin()
        if auth is None:
            return
        if not self._cs_ok():
            return self._err(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
        data = self._json_body()
        grant_id = str((data or {}).get("id") or "")
        row = STORE.get_grant(grant_id)
        if row is None:
            return self._err(HTTPStatus.NOT_FOUND, "授权不存在")
        if row["id"] == auth["grant"]["id"]:
            return self._err(HTTPStatus.BAD_REQUEST, "不能撤销当前正在使用的管理密钥")
        STORE.revoke_grant(grant_id)
        log(f"撤销授权  <- {self._client_ip()}  {grant_id}")
        self._send_json({"ok": True, "id": grant_id})

    def api_grants_rotate(self):
        auth = self._require_admin()
        if auth is None:
            return
        if not self._cs_ok():
            return self._err(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
        data = self._json_body()
        grant_id = str((data or {}).get("id") or "")
        row = STORE.get_grant(grant_id)
        if row is None:
            return self._err(HTTPStatus.NOT_FOUND, "授权不存在")
        secret = STORE.rotate_grant(grant_id)
        fresh = STORE.get_grant(grant_id)
        log(f"轮换密钥  <- {self._client_ip()}  {grant_id}")
        self._send_json({"ok": True, "grant": self._grant_public(fresh, secret)})

    # ------------------------------------------------------------ 传输（一次性分享）
    def api_transfers_create(self):
        """管理员创建一次传输：临时空间 + 只读令牌。之后用 ?space= 上传文件，取件用令牌登录。"""
        auth = self._require_admin()
        if auth is None:
            return
        if not self._cs_ok():
            return self._err(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
        data = self._json_body() or {}
        try:
            hours = float(data.get("expires_hours", 24) or 0)
            max_dl = int(data.get("max_downloads", 1) or 0)
            if hours < 0 or max_dl < 0:
                raise ValueError
        except (TypeError, ValueError):
            return self._err(HTTPStatus.BAD_REQUEST, "expires_hours / max_downloads 不合法")
        expires_at = time.time() + hours * 3600 if hours > 0 else None
        row, secret, space_id = STORE.create_transfer(
            label=str(data.get("label") or "")[:60], expires_at=expires_at,
            max_downloads=max_dl or None)
        log(f"新建传输  <- {self._client_ip()}  {row['id']}  max_downloads={max_dl or '∞'}")
        self._send_json({"ok": True, "transfer": {
            "id": row["id"], "space_id": space_id, "secret": secret,
            "expires_at": expires_at, "max_downloads": max_dl or None}},
            status=HTTPStatus.CREATED)

    def api_transfers_list(self):
        auth = self._require_admin()
        if auth is None:
            return
        self._send_json({"ok": True, "transfers": STORE.list_transfers()})

    def _transfer_grant(self, auth):
        g = auth["grant"]
        space = STORE.get_space(g["space_id"]) if g["space_id"] else None
        return g if space is not None and space["transient"] else None

    def api_done(self):
        """Compatibility signal only; pickup quotas are enforced from served byte ranges."""
        auth = self._auth()
        self._drain_body()
        if auth is None:
            return self._err(HTTPStatus.UNAUTHORIZED, "未登录")
        self._send_json({"ok": True})

    def api_spaces_list(self):
        auth = self._require_admin()
        if auth is None:
            return
        self._send_json({"ok": True, "spaces": [
            {"id": s["id"], "name": s["name"], "dir": s["dir"], "path": STORE.space_dir(s["id"]),
             "files": len(STORE.list_files(s["id"]))}
            for s in STORE.list_spaces()
        ], "public_url": PUBLIC_URL})

    def api_spaces_create(self):
        auth = self._require_admin()
        if auth is None:
            return
        if not self._cs_ok():
            return self._err(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
        data = self._json_body() or {}
        name = str(data.get("name") or "").strip()
        if not name:
            return self._err(HTTPStatus.BAD_REQUEST, "缺少空间名称")
        root = data.get("root") or None
        if root:
            root = os.path.abspath(os.path.expanduser(str(root)))
            try:
                os.makedirs(root, exist_ok=True)
            except OSError as exc:
                return self._err(HTTPStatus.BAD_REQUEST, f"目录不可用: {exc}")
        space_id = STORE.create_space(name, root)
        log(f"新建空间  <- {self._client_ip()}  {name}")
        self._send_json({"ok": True, "space": {
            "id": space_id, "name": name, "path": STORE.space_dir(space_id),
        }}, status=HTTPStatus.CREATED)

    # ------------------------------------------------------------ 日志
    def log_message(self, fmt, *args):
        pass


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="landrop serve",
        description="LAN Drop - 局域网文件收集与快捷分享（多空间 / 多链接 / 服务端校验）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  landrop serve --data-dir ./data\n"
            "  landrop serve --data-dir ./data --dir /srv/share\n"
            "  landrop serve --data-dir ./data --import ./uploads\n"
            "  landrop serve --data-dir ./data --admin-key '我的管理密钥'\n"
        ),
    )
    p.add_argument("--data-dir", default=None,
                   help="数据目录：state.sqlite3 / files / partial / trash（默认与桌面窗口相同的"
                        "系统数据目录；也可用环境变量 LANDROP_DATA_DIR）")
    p.add_argument("--dir", default="",
                   help="把默认空间的共享目录放到指定位置（默认 <data-dir>/files/<空间名>）")
    p.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    p.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    p.add_argument("--space", default="共享空间", help="默认空间名称（首次启动时创建）")
    p.add_argument("--admin-key", default=os.environ.get("LANDROP_ADMIN_KEY", ""),
                   help="管理员密钥；首次启动时生效，之后如需更换用 --reset-admin")
    p.add_argument("--reset-admin", action="store_true", help="用 --admin-key 重置管理员密钥")
    p.add_argument("--import", dest="import_dir", default="",
                   help="把已有目录导入默认空间（复制 + 校验，原目录不动）")
    p.add_argument("--import-recursive", action="store_true", help="导入时递归子目录")
    p.add_argument("--open-browser", action="store_true", help="启动后打开浏览器")
    p.add_argument("--public-url", default=os.environ.get("LANDROP_PUBLIC_URL", ""),
                   help="对外访问地址（默认自动探测）；容器内请设为宿主机局域网地址，"
                        "如 http://192.168.1.10:8800，否则分享链接会指向容器内网地址")
    p.add_argument("--no-banner", action="store_true", help="不打印启动横幅（GUI 用）")
    args = p.parse_args(argv)
    if args.reset_admin and not (args.admin_key or os.environ.get("LANDROP_ADMIN_KEY")):
        p.error("--reset-admin 需要同时提供 --admin-key（或环境变量 LANDROP_ADMIN_KEY）")
    return args


_janitor_started = False


def _start_janitor(interval: float = 60.0):
    """后台清理：过期会话与失效的一次性传输（连磁盘文件一起删）。
    进程内只启动一个：桌面窗口反复启停服务时不会越积越多（它总是清理当前的 STORE）。"""
    global _janitor_started
    if _janitor_started:
        return
    _janitor_started = True

    def loop():
        while True:
            time.sleep(interval)
            try:
                if STORE is not None:
                    STORE.purge_expired()
                    parts = STORE.purge_orphan_parts()
                    n = STORE.cleanup_transfers()
                    if n or parts:
                        log(f"已清理 {n} 个失效传输、{parts} 个孤儿分块")
            except Exception as exc:  # noqa: BLE001
                log(f"!! 清理任务出错: {exc!r}")
    threading.Thread(target=loop, daemon=True, name="janitor").start()


def prepare(data_dir: str, share_dir: str = "", space_name: str = "共享空间",
            admin_key: str = "", reset_admin: bool = False,
            import_dir: str = "", import_recursive: bool = False,
            host: str = "0.0.0.0", port: int = 8000, public_url: str = "") -> dict:
    """初始化数据目录、默认空间与管理员密钥；返回启动信息。"""
    global STORE, DEFAULT_SPACE, PUBLIC_URL
    STORE = Store(data_dir)
    STORE.purge_expired()
    STORE.cleanup_transfers()
    _start_janitor()
    exists = STORE.get_meta("default_space")
    if exists and STORE.get_space(exists):
        DEFAULT_SPACE = exists
        if share_dir:
            log("提示：默认空间已存在，--dir 只在首次创建空间时生效")
    else:
        sid = STORE.create_space(space_name, share_dir or None)
        STORE.set_meta("default_space", sid)
        DEFAULT_SPACE = sid

    # 容器内看到的网卡地址不是宿主机地址，允许用 --public-url / LANDROP_PUBLIC_URL 指定
    public = (public_url or os.environ.get("LANDROP_PUBLIC_URL") or "").strip()
    loopback = host in ("127.0.0.1", "localhost", "::1")    # 仅本机：链接里不能写局域网地址
    PUBLIC_URL = (public or f"http://{'127.0.0.1' if loopback else local_ip()}:{port}").rstrip("/")

    created_key = None
    admin = STORE.admin_grant()
    if admin is None or reset_admin:
        created_key, _ = STORE.init_admin(admin_key or None)
    else:
        STORE.init_admin(None)
        if STORE.admin_key_file_stale():
            log("⚠ admin-key.txt 与当前管理员密钥不一致（密钥可能已被轮换）。"
                "忘记密钥可用 --reset-admin --admin-key <新密钥> 重置。")

    import_report = None
    if import_dir:
        import_report = STORE.import_directory(DEFAULT_SPACE, import_dir, import_recursive)

    return {
        "data_dir": STORE.data_dir,
        "share_dir": STORE.space_dir(DEFAULT_SPACE),
        "space_id": DEFAULT_SPACE,
        "space_name": (STORE.get_space(DEFAULT_SPACE) or {})["name"],
        "admin_key": created_key,
        "admin_key_file": STORE.admin_key_file(),
        "host": host,
        "port": port,
        "url": PUBLIC_URL,
        "local_url": f"http://127.0.0.1:{port}",
        "import_report": import_report,
    }


class _Server(ThreadingHTTPServer):
    def server_bind(self):
        # 标准库的 HTTPServer.server_bind 会调用 socket.getfqdn() 做反向 DNS，
        # 在 macOS/无 DNS 环境里可能卡几十秒；这里只绑定，不做名字解析。
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    httpd = _Server((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def banner(info: dict):
    line = "=" * 60
    print(line)
    print(f"  {APP_NAME} v{VERSION} · 局域网文件收集与快捷分享")
    print(line)
    print(f"  访问地址 :  {info['url']}")
    print(f"  本机访问 :  {info['local_url']}")
    print(f"  管理面板 :  {info['local_url']}/admin")
    print(f"  共享目录 :  {info['share_dir']}")
    print(f"  数据目录 :  {info['data_dir']}")
    if info["admin_key"]:
        print(f"  管理员密钥: {info['admin_key']}   （已写入 {info['admin_key_file']}）")
    else:
        print(f"  管理员密钥: 见 {info['admin_key_file']}")
    print(line)
    print("  在管理面板创建分享链接，发给同一局域网的人即可开始收集。")
    print("  按 Ctrl+C 停止服务。")
    print(line, flush=True)


def main(argv=None):
    setup_console()
    args = parse_args(argv)
    data_dir, source = resolve_data_dir(args.data_dir)
    if source not in ("参数", "LANDROP_DATA_DIR"):
        log(f"数据目录：{data_dir}（{source}；可用 --data-dir 指定）")
    info = prepare(
        data_dir=data_dir, share_dir=args.dir, space_name=args.space,
        admin_key=args.admin_key, reset_admin=args.reset_admin,
        import_dir=args.import_dir, import_recursive=args.import_recursive,
        host=args.host, port=args.port, public_url=args.public_url,
    )
    if not args.no_banner:
        banner(info)
    if info["import_report"]:
        rep = info["import_report"]
        log(f"导入完成：成功 {len(rep['imported'])}，跳过 {len(rep['skipped'])}，"
            f"失败 {len(rep['failed'])}；清单 {rep['manifest']}")

    httpd = make_server(args.host, args.port)
    if args.open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(info["local_url"])).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())






