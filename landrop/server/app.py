"""A single LAN HTTP service. Management stays in the local SQLite control plane."""
from __future__ import annotations

import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import pkgutil
import re
import secrets
import socket
import socketserver
import sqlite3
import threading
import time
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from landrop.common import CHUNK, CliError, file_version, lan_addresses, stat_version
from .store import Store, TEXT_COUNT, TEXT_LIMIT

SESSION_COOKIE = "ld_session"
VISITOR_COOKIE = "ld_visitor"


class HTTPServer(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, address, runtime):
        self.runtime = runtime
        self._connections = set()
        self._transfers = set()
        self._mutex = threading.Lock()
        self._slots = threading.BoundedSemaphore(24)
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def get_request(self):
        conn, address = super().get_request()
        conn.settimeout(30)
        with self._mutex:
            self._connections.add(conn)
        return conn, address

    def process_request(self, request, client_address):
        if self.runtime._stopped.is_set() or not self._slots.acquire(False):
            self.close_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def close_request(self, request):
        with self._mutex:
            self._connections.discard(request)
        super().close_request(request)

    def interrupt(self):
        with self._mutex:
            for conn in list(self._connections):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class Runtime:
    def __init__(self, store):
        self.store = store
        self.httpd = self.thread = self._lease = None
        self._stopped = threading.Event()
        self._stopped.set()
        self._attempts = {}
        self._attempt_lock = threading.Lock()
        self.logger = logging.Logger("landrop")
        handler = RotatingFileHandler(os.path.join(store.data_dir, "diagnostics.log"),
                                      maxBytes=1024 * 1024, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(handler)

    @property
    def info(self):
        value = self.store.get_meta("service")
        return value if value and time.time() - value["heartbeat"] < 5 else None

    @property
    def running(self):
        return self.info is not None

    @property
    def active_transfers(self):
        if not self.httpd:
            return (self.info or {}).get("transfers", 0)
        with self.httpd._mutex:
            return len(self.httpd._transfers)

    def start(self, port=None, ip=None):
        if self.httpd:
            return self.info
        lease = sqlite3.connect(os.path.join(self.store.data_dir, "service.lock"),
                                timeout=0, isolation_level=None, check_same_thread=False)
        try:
            lease.execute("CREATE TABLE IF NOT EXISTS lock(id INTEGER)")
            lease.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            lease.close()
            if self.info:
                return self.info
            raise CliError("本机服务正在启动或停止，请稍后重试") from None
        settings = self.store.settings()
        address = ip if ip is not None else settings["ip"]
        port = settings["port"] if port is None else int(port)
        if address in ("", "0.0.0.0"):
            host, address = "0.0.0.0", lan_addresses()[0]
        elif address.startswith("127.") or address == "::1":
            host = address
        else:
            host = "::" if ":" in address else "0.0.0.0"
        try:
            self._stopped.clear()
            self.httpd = HTTPServer((host, port), self)
            port = self.httpd.server_address[1]
            address = f"[{address}]" if ":" in address else address
            self._identity = secrets.token_hex(12)
            self._info = {"id": self._identity, "pid": os.getpid(), "port": port,
                          "base": f"http://{address}:{port}", "heartbeat": time.time()}
            self._info["local_url"] = (f"http://127.0.0.1:{port}" if host == "0.0.0.0" else self._info["base"])
            self.store.set_meta("service", self._info)
            self._lease = lease
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            return dict(self._info)
        except OSError as exc:
            self._stopped.set()
            self.httpd = None
            lease.close()
            raise CliError(f"无法监听端口 {port}，请在设置中调整：{exc}") from None

    def _run(self):
        serving = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        serving.start()
        last_cleanup = 0
        try:
            while not self._stopped.wait(0.5):
                if self.store.get_meta("stop_service") == self._identity:
                    break
                self._info["heartbeat"] = time.time()
                self._info["transfers"] = self.active_transfers
                self.store.set_meta("service", self._info)
                if time.time() - last_cleanup > 30:
                    self.store.cleanup()
                    last_cleanup = time.time()
        except Exception:
            self.logger.exception("Service loop failed")
        finally:
            self._stopped.set()
            self.httpd.interrupt()
            self.httpd.shutdown()
            self.httpd.server_close()
            serving.join()
            if (self.store.get_meta("service") or {}).get("id") == self._identity:
                self.store.set_meta("service", None)
            self.httpd = None
            self._lease.close()
            self._lease = None
            self.store.close()

    def stop(self):
        if self.httpd:
            self._stopped.set()
            self.thread.join(30)
            if self.thread.is_alive():
                raise CliError("服务仍在停止，请稍候")
        elif self.info:
            self.store.set_meta("stop_service", self.info["id"])
            deadline = time.monotonic() + 30
            while self.info and time.monotonic() < deadline:
                time.sleep(0.1)
            if self.info:
                raise CliError("服务仍在停止，请稍候")

    def guard(self, auth, upload=False):
        if self._stopped.is_set():
            raise CliError("服务已停止", 403)
        return self.store.guard(auth, upload)

    def login_limit(self, address, failed=False):
        now = time.monotonic()
        with self._attempt_lock:
            if len(self._attempts) > 4096:
                self._attempts = {ip: times for ip, times in self._attempts.items()
                                  if times and now - times[-1] < 60}
            times = [stamp for stamp in self._attempts.get(address, []) if now - stamp < 60]
            if failed:
                times.append(now)
            self._attempts[address] = times
            return len(times) >= 10

    def close(self):
        if self.httpd:
            self.stop()
        for handler in self.logger.handlers:
            handler.close()


class ChunkedWriter:
    def __init__(self, handler, guard):
        self.handler, self.guard, self.position = handler, guard, 0

    def write(self, data):
        self.guard()
        if data:
            stream = self.handler.wfile
            stream.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.position += len(data)
        return len(data)

    def tell(self):
        return self.position

    def flush(self):
        self.handler.wfile.flush()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LANDrop"

    @property
    def runtime(self):
        return self.server.runtime

    @property
    def store(self):
        return self.runtime.store

    def log_message(self, *_args):
        pass

    def _headers(self, status, mime, length=None, **headers):
        self._started = True
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), str(value))

    def _json(self, data, status=200, cookies=(), **headers):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(raw), **headers)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _read(self, limit):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise CliError("请求大小不正确", 400) from None
        if self.headers.get("Transfer-Encoding") or not 0 <= size <= limit:
            raise CliError("请求内容过大或格式不支持", 413)
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise CliError("请求内容未完整到达", 400)
        return raw

    def _body(self):
        try:
            value = json.loads(self._read(16384) or b"{}")
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeDecodeError):
            raise CliError("请求格式不正确", 400) from None

    def _cookies(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return {}
        return {key: value.value for key, value in cookie.items()}

    def _cookie(self, key, value, age=7 * 86400):
        return f"{key}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}"

    def _guard(self, auth, upload=False):
        return self.runtime.guard(auth, upload)

    def _content(self, auth):
        share = self._guard(auth)
        settings = self.store.settings()
        files = self.store.files(share["id"])
        self._guard(auth)
        return {"share": {key: share[key] for key in ("id", "label", "allow_upload", "public_received", "expires_at")},
                "files": [{key: item[key] for key in ("id", "name", "path", "size", "version", "sha256")} for item in files],
                "texts": self.store.texts(share["id"]),
                "limits": {"text": TEXT_LIMIT, "text_count": TEXT_COUNT,
                           "text_preview": settings["text_preview_mib"] * 1024 * 1024,
                           "image_preview": settings["image_preview_mib"] * 1024 * 1024}}

    def _download(self, auth, params):
        self._guard(auth)
        item = self.store.file(auth["share"]["id"], params.get("id", [""])[0])
        if params.get("version", [item["version"]])[0] != item["version"]:
            raise CliError("文件版本已变化，请刷新后重新下载", 409)
        preview = params.get("preview", [""])[0]
        settings = self.store.settings()
        if preview in ("text", "image") and item["size"] > settings[preview + "_preview_mib"] * 1024 * 1024:
            raise CliError("超过预览上限，请下载文件", 413)
        digest = self.store.digest(item, lambda: self._guard(auth))
        size, start, stop = item["size"], 0, item["size"]
        value, ranged = self.headers.get("Range", ""), False
        if value and self.headers.get("If-Range", f'"{item["version"]}"') == f'"{item["version"]}"':
            ranged = True
            match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value)
            if not match or not any(match.groups()):
                raise CliError("断点范围不正确", 416)
            lo, hi = match.groups()
            start = int(lo) if lo else max(0, size - int(hi))
            stop = min(size, int(hi) + 1) if lo and hi else size
            if not 0 <= start < stop <= size:
                self._json({"error": "断点超出范围"}, 416, Content_Range=f"bytes */{size}")
                return
        mime = mimetypes.guess_type(item["name"])[0] or "application/octet-stream"
        inline = preview and (mime.startswith(("image/", "audio/", "video/")) and mime != "image/svg+xml")
        if preview == "text":
            mime = "text/plain; charset=utf-8"
        with open(item["full_path"], "rb") as source:
            if stat_version(os.fstat(source.fileno())) != item["version"]:
                raise CliError("文件版本已变化，请重试", 409)
            headers = {"Accept_Ranges": "bytes", "ETag": f'"{item["version"]}"', "X_File_SHA256": digest,
                       "Content_Disposition": ("inline" if inline else "attachment") +
                       "; filename*=UTF-8''" + quote(item["name"].split("/")[-1]),
                       "Content_Security_Policy": "sandbox; default-src 'none'"}
            if ranged:
                headers["Content_Range"] = f"bytes {start}-{stop - 1}/{size}"
            self._headers(206 if "Content_Range" in headers else 200, mime, stop - start, **headers)
            self.end_headers()
            if self.command == "HEAD":
                return
            source.seek(start)
            remaining = stop - start
            while remaining:
                self._guard(auth)
                if file_version(item["full_path"]) != item["version"]:
                    raise CliError("文件内容已变化", 409)
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise CliError("文件未完整读取", 409)
                self.wfile.write(block)
                remaining -= len(block)
            self._guard(auth)
            if file_version(item["full_path"]) != item["version"]:
                raise CliError("文件内容已变化", 409)

    def _zip(self, auth):
        if self.command == "GET":
            body = {}
        elif self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            try:
                fields = parse_qs(self._read(1024 * 1024).decode("utf-8"))
                body = {"ids": json.loads(fields["ids"][0])} if "ids" in fields else {}
            except (ValueError, KeyError):
                raise CliError("批量下载参数不正确", 400) from None
        else:
            body = self._body()
        files = self.store.files(auth["share"]["id"])
        ids = body.get("ids")
        if ids is not None:
            if (not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids)
                    or set(ids) - {item["id"] for item in files}):
                raise CliError("批量下载包含未授权的文件", 403)
            files = [item for item in files if item["id"] in ids]
        if not files:
            raise CliError("没有可下载的文件", 404)
        self._guard(auth)
        self._headers(200, "application/zip", Transfer_Encoding="chunked",
                      Content_Disposition="attachment; filename=LANDrop.zip")
        self.end_headers()
        writer = ChunkedWriter(self, lambda: self._guard(auth))
        with zipfile.ZipFile(writer, "w", compression=zipfile.ZIP_STORED) as archive:
            for item in files:
                with open(item["full_path"], "rb") as source, archive.open(item["name"], "w", force_zip64=True) as dest:
                    if stat_version(os.fstat(source.fileno())) != item["version"]:
                        raise CliError("文件内容已变化", 409)
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        self._guard(auth)
                        if file_version(item["full_path"]) != item["version"]:
                            raise CliError("文件内容已变化", 409)
                        dest.write(block)
                    if file_version(item["full_path"]) != item["version"]:
                        raise CliError("文件内容已变化", 409)
        self._guard(auth)
        self.wfile.write(b"0\r\n\r\n")

    def _handle(self):
        self._started = False
        transfer = False
        try:
            parsed = urlsplit(self.path)
            path, params = parsed.path, parse_qs(parsed.query)
            allowed = {"GET": {"/", "/logo.png", "/api/files", "/api/download", "/api/text", "/api/zip"},
                       "HEAD": {"/api/download"}, "POST": {"/api/login", "/api/logout", "/api/text", "/api/zip"},
                       "PUT": {"/api/upload"}, "DELETE": {"/api/upload"}}
            if path not in allowed.get(self.command, set()):
                raise CliError("接口不存在", 404)
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != host:
                raise CliError("不允许跨站请求", 403)
            if self.command in ("POST", "PUT", "DELETE") and self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise CliError("不允许跨站请求", 403)
            if path in ("/", "/logo.png"):
                name = "index.html" if path == "/" else "logo.png"
                raw = pkgutil.get_data("landrop.server", "static/" + name)
                self._headers(200, "text/html; charset=utf-8" if path == "/" else "image/png", len(raw))
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                 "style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; "
                                 "frame-ancestors 'none'; base-uri 'none'")
                self.end_headers()
                self.wfile.write(raw)
                return
            cookies = self._cookies()
            if path == "/api/login":
                if self.runtime.login_limit(self.client_address[0]):
                    self._json({"error": "尝试过多，请稍后重试"}, 429, Retry_After=60)
                    self.close_connection = True
                    return
                body = self._body()
                visitor = cookies.get(VISITOR_COOKIE, "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", visitor):
                    visitor = secrets.token_urlsafe(24)
                try:
                    code = body.get("code", "")
                    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
                        raise CliError("请输入六位数字授权码", 401)
                    token = self.store.login(code, visitor)
                except CliError as exc:
                    self.runtime.login_limit(self.client_address[0], failed=True)
                    raise CliError(str(exc), 401) from None
                auth = self.store.auth(token)
                self._json(self._content(auth), cookies=(self._cookie(SESSION_COOKIE, token),
                                                         self._cookie(VISITOR_COOKIE, visitor, 400 * 86400)))
                return
            if path == "/api/logout":
                self.store.logout(cookies.get(SESSION_COOKIE, ""))
                self._json({"ok": True}, cookies=(self._cookie(SESSION_COOKIE, "", 0),))
                return
            auth = self.store.auth(cookies.get(SESSION_COOKIE, ""))
            if not auth:
                raise CliError("请重新输入有效授权码", 401)
            expected_share = self.headers.get("X-LANDrop-Share") or params.get("share", [""])[0]
            if expected_share and expected_share != auth["share"]["id"]:
                raise CliError("当前分享已切换，请重新授权", 409)
            if path in ("/api/download", "/api/zip", "/api/upload") and self.command != "DELETE":
                transfer = True
                with self.server._mutex:
                    self.server._transfers.add(threading.current_thread())
            if path == "/api/files":
                self._json(self._content(auth))
            elif path == "/api/download":
                self._download(auth, params)
            elif path == "/api/zip":
                self._zip(auth)
            elif path == "/api/text":
                if self.command == "POST":
                    self._guard(auth, upload=True)
                    try:
                        content = self._read(TEXT_LIMIT).decode("utf-8")
                    except UnicodeDecodeError:
                        raise CliError("文本编码须为 UTF-8", 400) from None
                    text_id = self.store.add_text(auth["share"]["id"], content, auth,
                                                  lambda: self._guard(auth, upload=True))
                    self._json({"id": text_id})
                else:
                    raw = self.store.text(auth["share"]["id"], params.get("id", [""])[0]).encode("utf-8")
                    self._guard(auth)
                    self._headers(200, "text/plain; charset=utf-8", len(raw))
                    self.end_headers()
                    self.wfile.write(raw)
            elif self.command == "DELETE":
                self._guard(auth)
                self.store.abort_upload(auth, params.get("id", [""])[0])
                self._json({"ok": True})
            else:
                self._guard(auth, upload=True)
                try:
                    total, offset = int(params.get("total", ["-1"])[0]), int(params.get("offset", ["0"])[0])
                except ValueError:
                    raise CliError("上传参数不正确", 400) from None
                result = self.store.upload(auth, params.get("id", [""])[0],
                                           params.get("path", params.get("name", [""]))[0], total,
                                           params.get("sha256", [""])[0], offset, self._read(CHUNK),
                                           lambda: self._guard(auth, upload=True))
                if result.pop("conflict", False):
                    self._json(result, 409, X_Expected_Offset=result["offset"])
                else:
                    self._json(result)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except CliError as exc:
            self.close_connection = True
            if not self._started:
                self._json({"error": str(exc)}, exc.status or 400)
        except Exception:
            self.runtime.logger.exception("Request failed: %s %s", self.command, self.path.partition("?")[0])
            self.close_connection = True
            if not self._started:
                self._json({"error": "服务内部错误，请查看本机诊断日志"}, 500)
        finally:
            if transfer:
                with self.server._mutex:
                    self.server._transfers.discard(threading.current_thread())
            self.store.close()

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _handle
