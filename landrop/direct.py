"""直连发送（去中心化）：进程内的临时 HTTP 监听，配合 ``landrop get`` 取件。仅标准库。"""
from __future__ import annotations

import hmac
import html
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
import zipfile

from .common import CliError, collect_entries, human, merge_ranges, sha256_file


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

    文件码 ``地址/令牌`` 也是一个网址：没装 LAN Drop 的设备（比如手机）用浏览器打开就能下载。

    一次性：全部接收者取完（命令行调用 /api/done，或浏览器把文件都下载完）、超时或失败次数过多后自动关闭。
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
        self._complete: dict[str, set[str]] = {}   # 会话 → 已完整送达的文件 id
        self._ranges: dict = {}
        self._counted: set[str] = set()            # 已算作「取完」的会话
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
        warnings = []
        entries = collect_entries(paths, warnings.append)
        return ([{"id": str(i), "path": full, "name": rel, "size": os.path.getsize(full)}
                 for i, (full, rel) in enumerate(entries)], warnings)

    def prepare_hashes(self, on_progress=None):
        try:
            for e in self.entries:
                if self.finished.is_set():
                    break
                try:
                    e["sha256"] = sha256_file(e["path"])
                except OSError as exc:
                    self.hash_error = str(exc)
                if on_progress:
                    on_progress(e["name"])
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

    # ---- 取件计数
    def _mark_complete(self, sid: str, ids):
        with self._lock:
            got = self._complete.setdefault(sid, set())
            got.update(ids)
            whole = len(got) >= len(self.entries)
        if whole:
            self._count_done(sid)

    def _count_done(self, sid: str):
        """一个接收者取完：同一会话只算一次（浏览器下载完 + 命令行的 /api/done 不会重复计数）。"""
        with self._lock:
            if sid in self._counted or len(self._complete.get(sid, set())) < len(self.entries):
                return
            self._counted.add(sid)
            self._done += 1
            complete = self._done >= self.receivers
        self.on_event("接收完成")
        if complete:
            self.stop("done")

    def _record_range(self, sid, entry, start, stop):
        if stop <= start and entry["size"]:
            return
        with self._lock:
            key = (sid, entry["id"])
            ranges = self._ranges[key] = merge_ranges(self._ranges.get(key, []), start, stop)
            complete = not entry["size"] or ranges == [[0, entry["size"]]]
        if complete:
            self._mark_complete(sid, [entry["id"]])

    def _fail_attempt(self):
        time.sleep(0.3)
        with self._lock:
            self._failures += 1
            exceeded = self._failures >= self.max_failures
        if exceeded:
            self.on_event("错误密钥尝试次数过多，已关闭发送")
            self.stop("failures")

    def _new_session(self, peer: str) -> str:
        sid = secrets.token_urlsafe(16)
        with self._lock:
            self._sessions.add(sid)
        self.on_event(f"接收端已连接：{peer}")
        return sid

    # ---- HTTP
    def _make_handler(self):
        sender = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "LANDropDirect"

            def log_message(self, fmt, *args):
                pass

            def _send(self, status, body: bytes, ctype: str, headers=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, obj, status=200, headers=None):
                self._send(status, json.dumps(obj).encode(), "application/json", headers)

            def _html(self, body: str, status=200, headers=None):
                h = {"Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                                "img-src data:; base-uri 'none'; form-action 'none'"}
                h.update(headers or {})
                self._send(status, body.encode("utf-8"), "text/html; charset=utf-8", h)

            def _body(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                return self.rfile.read(min(n, 65536)) if n > 0 else b""

            def _sid(self):
                jar = self.headers.get("Cookie") or ""
                m = re.search(r"ld_direct=([A-Za-z0-9_-]+)", jar)
                return m.group(1) if m and m.group(1) in sender._sessions else None

            def _cookie(self, sid):
                return {"Set-Cookie": f"ld_direct={sid}; Path=/; HttpOnly; SameSite=Lax"}

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
                        sid = self._sid()
                        if sid in sender._counted or sid is None:
                            sid = sender._new_session(self.client_address[0])
                        return self._json({"ok": True, "kind": "direct", "perm": "read",
                                           "label": "direct", "range_progress": True},
                                          headers=self._cookie(sid))
                    self._json({"ok": False, "error": "密钥错误"}, 401)
                    sender._fail_attempt()
                    return
                if path == "/api/done":
                    self._body()
                    sid = self._sid()
                    if sid is None:
                        return self._json({"ok": False, "error": "未登录"}, 401)
                    with sender._lock:
                        complete = len(sender._complete.get(sid, set())) == len(sender.entries)
                    self._json({"ok": True, "complete": complete})
                    sender._count_done(sid)
                    return
                self._json({"ok": False, "error": "not found"}, 404)

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                if not path.startswith("/api/"):
                    return self._page(path)
                sid = self._sid()
                if sid is None:
                    return self._json({"ok": False, "error": "未登录"}, 401)
                if not sender._ready.is_set():
                    return self._not_ready()
                if path == "/api/files":
                    return self._json({"ok": True, "files": [
                        {"id": e["id"], "name": e["name"], "size": e["size"],
                         "sha256": e.get("sha256", ""), "mtime": 0} for e in sender.entries]})
                if path == "/api/download":
                    return self._download(parsed, sid)
                if path == "/api/zip":
                    return self._zip(sid)
                self._json({"ok": False, "error": "not found"}, 404)

            # -- 浏览器取件页：文件码就是网址
            def _page(self, path):
                token = path.strip("/")
                if token and hmac.compare_digest(token.encode(), sender.token.encode()):
                    if not sender._ready.is_set():
                        return self._html(_message_page("正在准备文件", "请稍候刷新此页面。"), 503,
                                          headers={"Retry-After": "2"})
                    sid = self._sid() or sender._new_session(self.client_address[0])
                    return self._html(_landing_page(sender), headers=self._cookie(sid))
                if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", token):     # 像令牌却不对：算一次失败
                    self._html(_message_page("文件码不正确", "请检查链接是否完整，或让对方重新发送。"), 404)
                    sender._fail_attempt()
                    return
                self._html(_message_page("LAN Drop 直连发送",
                                         "请使用对方给你的完整文件码链接（形如 地址:端口/令牌）。"), 404)

            def _download(self, parsed, sid):
                fid = (urllib.parse.parse_qs(parsed.query).get("id") or [""])[0]
                entry = next((e for e in sender.entries if e["id"] == fid), None)
                if entry is None:
                    return self._json({"ok": False, "error": "文件不存在"}, 404)
                if urllib.parse.parse_qs(parsed.query).get("progress") == ["1"]:
                    with sender._lock:
                        ranges = list(sender._ranges.get((sid, fid), []))
                    return self._json({"ok": True, "ranges": ranges})
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
                self.send_header("Content-Disposition",
                                 "attachment; " + _disposition(entry["name"].rsplit("/", 1)[-1]))
                self.send_header("Accept-Ranges", "bytes")
                if entry.get("sha256"):
                    self.send_header("X-File-SHA256", entry["sha256"])
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if self.command == "HEAD":
                    return
                left = length
                try:
                    with open(entry["path"], "rb") as f:
                        f.seek(start)
                        while left > 0:
                            chunk = f.read(min(1024 * 1024, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    sender._record_range(sid, entry, start, start + length - left)
                if left == 0:
                    sender.on_event(f"已发送：{entry['name']}  ({human(size)})")

            def _zip(self, sid):
                """全部文件打成一个 zip 边读边发（不落临时文件；存储模式，不压缩）。"""
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", "attachment; " + _disposition(_zip_name(sender)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command == "HEAD":
                    return
                self.close_connection = True
                try:
                    with zipfile.ZipFile(_Unseekable(self.wfile), "w", zipfile.ZIP_STORED) as zf:
                        for e in sender.entries:
                            with open(e["path"], "rb") as src, \
                                    zf.open(e["name"], "w", force_zip64=True) as dst:
                                for block in iter(lambda: src.read(1024 * 1024), b""):
                                    dst.write(block)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                sender.on_event(f"已打包发送全部 {len(sender.entries)} 个文件（{human(sender.total_bytes)}）")
                sender._mark_complete(sid, [e["id"] for e in sender.entries])

        return Handler


class _Unseekable:
    """给 zipfile 的只写流：socket 不能 seek/tell，zipfile 会改用数据描述符。"""

    def __init__(self, raw):
        self.raw = raw

    def write(self, data):
        self.raw.write(data)
        return len(data)

    def flush(self):
        self.raw.flush()

    def tell(self):
        raise OSError("unseekable")

    def seekable(self):
        return False


def _disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "ignore").decode("ascii").replace('"', "'") or "download"
    return f"filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


def _zip_name(sender) -> str:
    tops = {e["name"].split("/", 1)[0] for e in sender.entries}
    return (tops.pop() if len(tops) == 1 else "LAN Drop") + ".zip"


_PAGE_CSS = """
:root{--bg:#f4f3f0;--card:#fff;--text:#15171c;--muted:#6b6f78;--line:rgba(22,24,29,.09);
--accent:#0fc39d;--accent-ink:#032019;--accent-soft:rgba(15,195,157,.13);color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#08090c;--card:#14161b;--text:#eceef2;--muted:#9097a3;
--line:rgba(255,255,255,.08);--accent-soft:rgba(25,207,168,.16)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei UI",
"Noto Sans CJK SC",sans-serif;padding:max(24px,env(safe-area-inset-top)) 16px 40px}
main{max-width:560px;margin:0 auto}.brand{display:flex;align-items:center;gap:10px;font-weight:650;
margin-bottom:20px}.logo{width:30px;height:30px;border-radius:9px;background:var(--accent);
display:grid;place-items:center}.logo:after{content:"";width:11px;height:14px;background:#052019;
border-radius:50% 50% 50% 50%/60% 60% 40% 40%}
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:20px;
box-shadow:0 20px 44px -30px rgba(0,0,0,.35)}h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 16px;font-size:14px}ul{list-style:none;margin:0;padding:0}
li{display:flex;align-items:center;gap:12px;padding:12px 0;border-top:1px solid var(--line)}
.name{flex:1;min-width:0;overflow-wrap:anywhere}.size{color:var(--muted);font-size:13px;white-space:nowrap}
a.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;text-decoration:none;
border-radius:12px;font-weight:600;padding:8px 14px;background:var(--accent-soft);color:var(--text);
white-space:nowrap}a.btn.primary{background:var(--accent);color:var(--accent-ink);width:100%;
padding:13px;margin:4px 0 16px;font-size:16px}.note{color:var(--muted);font-size:13px;margin-top:16px}
"""


def _page_shell(title: str, inner: str) -> str:
    return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
            f"<title>{html.escape(title)} · LAN Drop</title><style>{_PAGE_CSS}</style></head><body><main>"
            "<div class='brand'><span class='logo'></span>LAN Drop</div>"
            f"<div class='card'>{inner}</div></main></body></html>")


def _landing_page(sender) -> str:
    n = len(sender.entries)
    rows = "".join(
        f"<li><span class='name'>{html.escape(e['name'])}</span>"
        f"<span class='size'>{human(e['size'])}</span>"
        f"<a class='btn' href='/api/download?id={e['id']}'>下载</a></li>"
        for e in sender.entries[:500])
    more = f"<li><span class='name'>…还有 {n - 500} 个文件，请用「全部下载」</span></li>" if n > 500 else ""
    zip_btn = (f"<a class='btn primary' href='/api/zip'>全部下载（{human(sender.total_bytes)}，zip）</a>"
               if n > 1 else "")
    return _page_shell("收到文件", (
        f"<h1>有人给你发了 {n} 个文件</h1>"
        f"<p class='sub'>共 {human(sender.total_bytes)} · 直接从对方电脑下载，不经过任何服务器</p>"
        f"{zip_btn}<ul>{rows}{more}</ul>"
        "<p class='note'>一次性链接：全部下载完成后，对方的发送会自动结束。"
        "装了 LAN Drop 的电脑可以在「接收」页粘贴文件码，或运行 <code>landrop get</code>，"
        "会校验 SHA-256 并支持断点续传。</p>"))


def _message_page(title: str, text: str) -> str:
    return _page_shell(title, f"<h1>{html.escape(title)}</h1><p class='sub'>{html.escape(text)}</p>")
