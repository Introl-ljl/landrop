"""直连发送（去中心化）：进程内的临时 HTTP 监听，配合 ``landrop get`` 取件。仅标准库。"""
from __future__ import annotations

import hmac
import http.server
import json
import os
import re
import secrets
import socket
import socketserver
import threading
import time
import urllib.parse

from .common import CliError, human, sha256_file


def _hostname_ips(timeout: float) -> list[str]:
    """按主机名解析本机地址（多网卡时用来列出备选）。解析可能走 DNS 而卡很久，所以限时。"""
    result: list[str] = []

    def work():
        try:
            result.extend(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return list(result)


def lan_addresses() -> list[str]:
    """本机可能被局域网访问到的 IPv4 地址，首选地址排第一（装了 docker/VPN 时可能不止一个）。"""
    found: list[str] = []
    try:                       # 不真正发包：只让系统选出默认路由的出口地址
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    for ip in _hostname_ips(1.0):
        if ip not in found:
            found.append(ip)
    found = [ip for ip in found if not ip.startswith("127.")]
    return found or ["127.0.0.1"]


class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):     # 跳过 getfqdn 反向 DNS（macOS/无 DNS 环境会卡很久）
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class DirectSender:
    """进程内的临时发送服务：说与 LAN Drop 服务端相同的最小 API（login / files / download），
    所以接收端的 ``get`` 不需要区分对面是直连发送端还是常驻服务。

    一次性：全部接收者完成（/api/done）、超时或失败次数过多后自动关闭。
    """

    def __init__(self, paths: list[str], token: str | None = None, host: str = "0.0.0.0",
                 port: int = 0, receivers: int = 1, max_failures: int = 5, on_event=None):
        self.token = token or secrets.token_urlsafe(12)
        self.receivers = max(1, receivers)
        self.max_failures = max_failures
        self.on_event = on_event or (lambda msg: None)
        self.entries, self.warnings = self._collect(paths)
        if not self.entries:
            raise CliError("没有可发送的文件")
        self.finished = threading.Event()
        self.reason = ""
        self._lock = threading.Lock()
        self._sessions: set[str] = set()
        self._sent: dict[str, set[str]] = {}    # 会话 → 已完整发出的文件 id（done 只认取齐的）
        self._failures = 0
        self._done = 0
        self._ready = threading.Event()         # 校验值就绪前，登录/下载返回 503 让接收端等待
        self.hash_error = ""
        self._hash_cache: dict[int, str] = {}
        self.httpd = _QuietServer((host, port), self._make_handler())
        self.port = self.httpd.server_address[1]

    # ---- 文件清单
    @staticmethod
    def _collect(paths):
        entries, seen = [], set()
        for p in paths:
            p = os.path.abspath(p)
            if os.path.isdir(p):
                top = os.path.basename(p.rstrip("/\\")) or "folder"
                for root, dirs, files in os.walk(p):
                    dirs.sort()
                    for fn in sorted(files):
                        full = os.path.join(root, fn)
                        if os.path.isfile(full):
                            rel = top + "/" + os.path.relpath(full, p).replace(os.sep, "/")
                            entries.append((full, rel))
            elif os.path.isfile(p):
                entries.append((p, os.path.basename(p)))
        out, used, warnings = [], set(), []
        for full, rel in entries:
            if rel in used:
                # 同名文件不能静默丢弃：先带上父目录名区分，再不行就加序号
                parent = os.path.basename(os.path.dirname(full.rstrip("/\\")))
                alt = f"{parent}/{rel}" if parent and f"{parent}/{rel}" not in used else ""
                if alt:
                    warnings.append(f"重名：{rel} → 以 {alt} 发送")
                else:
                    stem, ext = os.path.splitext(rel)
                    i = 1
                    while f"{stem} ({i}){ext}" in used:
                        i += 1
                    alt = f"{stem} ({i}){ext}"
                    warnings.append(f"重名：{rel} → 以 {alt} 发送")
                rel = alt
            used.add(rel)
            out.append({"id": str(len(out)), "path": full, "name": rel,
                        "size": os.path.getsize(full)})
        return out, warnings

    def prepare_hashes(self, on_progress=None):
        try:
            for e in self.entries:
                e["sha256"] = sha256_file(e["path"])
                if on_progress:
                    on_progress(e["name"])
        except OSError as exc:      # 文件中途消失等：发送继续，只是该文件没有校验值
            self.hash_error = str(exc)
        finally:
            self._ready.set()

    def start_hashing(self, on_progress=None):
        """后台计算校验值，不再阻塞监听；就绪前接收端登录会得到 503 并自动等待。"""
        threading.Thread(target=self.prepare_hashes, args=(on_progress,), daemon=True).start()

    @property
    def total_bytes(self) -> int:
        return sum(e["size"] for e in self.entries)

    # ---- 生命周期
    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def wait(self, timeout: float | None) -> str:
        """阻塞到结束；返回原因 done / timeout / failures / cancelled。"""
        if not self.finished.wait(timeout):
            self.stop("timeout")
        return self.reason

    def stop(self, reason: str = "cancelled"):
        with self._lock:
            if self.finished.is_set():
                return
            self.reason = reason
            self.finished.set()
        threading.Thread(target=self._shutdown, daemon=True).start()

    def _shutdown(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass

    # ---- HTTP
    def _make_handler(self):
        sender = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "LANDropDirect"

            def log_message(self, fmt, *args):
                pass

            def _json(self, obj, status=200, headers=None):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                return self.rfile.read(min(n, 65536)) if n > 0 else b""

            def _session(self):
                jar = self.headers.get("Cookie") or ""
                m = re.search(r"ld_direct=([A-Za-z0-9_-]+)", jar)
                return m.group(1) if m and m.group(1) in sender._sessions else None

            def _authed(self):
                return self._session() is not None

            def _not_ready(self):
                return self._json({"ok": False, "error": "发送端正在准备校验值，请稍候"},
                                  503, headers={"Retry-After": "2"})

            def do_POST(self):
                path = urllib.parse.urlparse(self.path).path
                if path == "/api/login":
                    if not sender._ready.is_set():
                        return self._not_ready()
                    try:
                        key = str(json.loads(self._body() or b"{}").get("key") or "")
                    except ValueError:
                        key = ""
                    if hmac.compare_digest(key.encode(), sender.token.encode()):
                        sid = secrets.token_urlsafe(16)
                        with sender._lock:
                            sender._sessions.add(sid)
                        sender.on_event(f"接收端已连接：{self.client_address[0]}")
                        return self._json({"ok": True, "kind": "direct", "perm": "read",
                                           "label": "direct"},
                                          headers={"Set-Cookie": f"ld_direct={sid}; Path=/; HttpOnly"})
                    time.sleep(0.3)
                    with sender._lock:
                        sender._failures += 1
                        exceeded = sender._failures >= sender.max_failures
                    self._json({"ok": False, "error": "密钥错误"}, 401)
                    if exceeded:
                        sender.on_event("错误密钥尝试次数过多，已关闭发送")
                        sender.stop("failures")
                    return
                if path == "/api/done":
                    self._body()
                    sid = self._session()
                    if not sid:
                        return self._json({"ok": False, "error": "未登录"}, 401)
                    all_ids = {e["id"] for e in sender.entries}
                    with sender._lock:
                        # 只有取齐全部文件的接收者才消耗一个名额：--only 部分取件不算完成
                        complete_fetch = all_ids <= sender._sent.get(sid, set())
                        complete = False
                        if complete_fetch:
                            sender._sent.pop(sid, None)
                            sender._done += 1
                            complete = sender._done >= sender.receivers
                    self._json({"ok": True, "complete": complete_fetch})
                    if complete_fetch:
                        sender.on_event("接收完成")
                        if complete:
                            sender.stop("done")
                    return
                self._json({"ok": False, "error": "not found"}, 404)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if not self._authed():
                    return self._json({"ok": False, "error": "未登录"}, 401)
                if not sender._ready.is_set():
                    return self._not_ready()
                if parsed.path == "/api/files":
                    return self._json({"ok": True, "files": [
                        {"id": e["id"], "name": e["name"], "size": e["size"],
                         "sha256": e.get("sha256", ""), "mtime": 0} for e in sender.entries]})
                if parsed.path == "/api/download":
                    return self._download(parsed)
                self._json({"ok": False, "error": "not found"}, 404)

            def _download(self, parsed):
                fid = (urllib.parse.parse_qs(parsed.query).get("id") or [""])[0]
                entry = next((e for e in sender.entries if e["id"] == fid), None)
                if entry is None:
                    return self._json({"ok": False, "error": "文件不存在"}, 404)
                size = entry["size"]
                start, end, partial = 0, max(size - 1, 0), False
                rng = self.headers.get("Range") or ""
                m = re.match(r"bytes=(\d*)-(\d*)$", rng)
                if size and m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        if m.group(2):
                            end = min(int(m.group(2)), size - 1)
                    else:
                        start = max(0, size - int(m.group(2)))
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    partial = True
                length = end - start + 1 if size else 0
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if entry.get("sha256"):
                    self.send_header("X-File-SHA256", entry["sha256"])
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                try:
                    with open(entry["path"], "rb") as f:
                        f.seek(start)
                        left = length
                        while left > 0:
                            chunk = f.read(min(1024 * 1024, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                    if end >= size - 1:
                        sid = self._session()   # 记录该会话已完整发出的文件，供 /api/done 判定
                        if sid:
                            with sender._lock:
                                sender._sent.setdefault(sid, set()).add(entry["id"])
                        sender.on_event(f"已发送：{entry['name']}  ({human(size)})")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler
