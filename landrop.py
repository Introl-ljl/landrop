#!/usr/bin/env python3
"""LAN Drop 命令行：把文件变成一个"文件码 + 一条取件命令"。

发送端（服务所在机器，或能访问服务并持有管理员密钥的机器）：
    landrop.py send a.zip b.png            # 上传并生成文件码
    landrop.py send                        # 不带参数：在当前目录里交互选择

接收端（同一局域网内任意机器，只需要本文件 + Python 3.8+）：
    landrop.py get <文件码>                 # 下载到当前目录
    landrop.py get <文件码> -o ~/Downloads  # 指定目录

文件码形如 ``192.168.1.5:8000/<令牌>``，令牌只对这一批文件有效，可设置有效期，
也可用 ``landrop.py revoke <授权ID>`` 提前撤销。仅使用 Python 标准库。

平台分工：Linux 使用本命令行；Windows / macOS 使用桌面启动器 ``desktop.py`` 里的
「发送 / 接收」页（图形界面，底层同样调用本文件的 ``send_files`` / ``fetch_files``）。
本文件本身在三个平台都能运行，接收端只需拷走这一个文件。
``landrop.py serve [server.py 的参数]`` 可直接启动服务（需要与 server.py 放在一起）。
"""
from __future__ import annotations

import argparse
import glob
import hmac
import http.server
import socket
import socketserver
import threading
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CHUNK = 8 * 1024 * 1024
DEFAULT_URL = "http://127.0.0.1:8000"


class CliError(Exception):
    pass


def setup_console():
    """Windows 控制台默认是 GBK 等代码页：强制 UTF-8 输出，避免中文文件名/提示乱码或崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def default_data_dir() -> str:
    """与 desktop.py 的数据目录约定一致，便于本机直接读到 admin-key.txt。"""
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LANDrop")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/LANDrop")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "landrop")


def cmd_prefix() -> str:
    """生成给接收端复制的命令前缀：打包版用可执行文件名，源码版按平台选解释器。"""
    if getattr(sys, "frozen", False):
        return "landrop-cli"
    return "python landrop.py" if sys.platform.startswith("win") else "python3 landrop.py"


# ------------------------------------------------------------------ 工具
def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def progress(label: str, done: int, total: int):
    if not sys.stderr.isatty():
        return
    pct = 100 if total == 0 else done * 100 // total
    sys.stderr.write(f"\r  {label[:40]:<40} {pct:3d}%  {human(done)}/{human(total)}   ")
    sys.stderr.flush()


def progress_end():
    if sys.stderr.isatty():
        sys.stderr.write("\n")


def normalize_base(url: str) -> str:
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url):
        url = "http://" + url
    return url


# ------------------------------------------------------------------ 文件码
def make_code(base: str, token: str) -> str:
    """文件码 = 服务地址(去掉 http://) + '/' + 令牌；https 地址保留协议前缀。"""
    host = base[len("http://"):] if base.startswith("http://") else base
    return f"{host}/{token}"


def parse_code(code: str) -> tuple[str, str]:
    code = code.strip()
    m = re.match(r"^(https?://)?([^/\s]+)/([A-Za-z0-9_-]+)$", code)
    if not m:
        raise CliError("文件码格式不对，应形如 192.168.1.5:8000/令牌")
    return (m.group(1) or "http://") + m.group(2), m.group(3)


def extract_code(text: str) -> str:
    """从粘贴内容里抠出文件码：整条取件命令、带引号的、多余空白都能认。"""
    m = re.search(r"(?:https?://)?[\w.\-\[\]:]+:\d+/[A-Za-z0-9_-]+", text or "")
    return m.group(0) if m else (text or "").strip()


# ------------------------------------------------------------------ HTTP 会话
class Client:
    def __init__(self, base: str):
        self.base = normalize_base(base)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method, path, body=None, headers=None, timeout=60, raw=False):
        data = None
        hdrs = dict(headers or {})
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        elif body is not None:
            data = body
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            resp = self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                msg = json.loads(payload).get("error") or payload.decode(errors="replace")
            except Exception:
                msg = payload.decode(errors="replace")
            err = CliError(f"{method} {path.split('?')[0]} → {exc.code} {msg}".strip())
            err.status = exc.code  # type: ignore[attr-defined]
            err.headers = exc.headers  # type: ignore[attr-defined]
            raise err from None
        except urllib.error.URLError as exc:
            raise CliError(f"连接不上 {self.base}：{exc.reason}") from None
        if raw:
            return resp
        with resp:
            payload = resp.read()
        return json.loads(payload) if payload else {}

    def login(self, secret: str) -> dict:
        return self.request("POST", "/api/login", {"key": secret})


# ------------------------------------------------------------------ 发送
def find_admin_key(args) -> str:
    if args.key:
        return args.key
    if os.environ.get("LANDROP_ADMIN_KEY"):
        return os.environ["LANDROP_ADMIN_KEY"]
    return read_admin_key(args.data_dir)


def read_admin_key(data_dir: str | None = None) -> str:
    """依次尝试：指定目录 → LANDROP_DATA_DIR → ./data → 平台默认数据目录。"""
    dirs = [data_dir, os.environ.get("LANDROP_DATA_DIR"), "./data", default_data_dir()]
    for d in filter(None, dirs):
        try:
            with open(os.path.join(d, "admin-key.txt"), encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
        except OSError:
            continue
    raise CliError("找不到管理员密钥：请用 --key、环境变量 LANDROP_ADMIN_KEY，"
                   "或 --data-dir 指向含 admin-key.txt 的数据目录")


def pick_interactively() -> list[str]:
    if not sys.stdin.isatty():
        raise CliError("没有指定文件。用法：landrop.py send 文件1 [文件2 ...]")
    files = sorted(p for p in os.listdir(".") if os.path.isfile(p))
    if not files:
        raise CliError("当前目录没有文件可选")
    print("当前目录的文件：")
    for i, name in enumerate(files, 1):
        print(f"  {i:>3}. {name}  ({human(os.path.getsize(name))})")
    raw = input("选择编号（如 1 3 5-7，a=全部）：").strip().lower()
    if raw in ("a", "all", "*"):
        return files
    chosen: list[str] = []
    for part in raw.replace(",", " ").split():
        lo, _, hi = part.partition("-")
        try:
            a, b = int(lo), int(hi or lo)
        except ValueError:
            raise CliError(f"看不懂的编号：{part}") from None
        for n in range(a, b + 1):
            if not 1 <= n <= len(files):
                raise CliError(f"编号超出范围：{n}")
            if files[n - 1] not in chosen:
                chosen.append(files[n - 1])
    if not chosen:
        raise CliError("没有选择任何文件")
    return chosen


def expand_paths(items: list[str], allow_dirs: bool = False) -> list[str]:
    out: list[str] = []
    for item in items:
        item = os.path.expanduser(item)
        matches = sorted(glob.glob(item)) if any(c in item for c in "*?[") else [item]
        if not matches:
            raise CliError(f"没有匹配：{item}")
        for m in matches:
            if os.path.isdir(m) and not allow_dirs:
                raise CliError(f"{m} 是目录；请先打包（如 tar/zip）或用通配符选文件")
            if not (os.path.isfile(m) or os.path.isdir(m)):
                raise CliError(f"文件不存在：{m}")
            ap = os.path.abspath(m)
            if ap not in out:
                out.append(ap)
    return out


def upload_file(cli: Client, space_id: str, path: str, on_progress=progress):
    name = os.path.basename(path)
    total = os.path.getsize(path)
    digest = sha256_file(path)
    upload_id = secrets.token_hex(8)
    offset = 0
    q = lambda off: "/api/upload?" + urllib.parse.urlencode({  # noqa: E731
        "name": name, "id": upload_id, "offset": off, "total": total,
        "sha256": digest, "space": space_id})
    with open(path, "rb") as f:
        while True:
            f.seek(offset)
            block = f.read(CHUNK)
            try:
                res = cli.request("PUT", q(offset), block,
                                  {"Content-Type": "application/octet-stream"}, timeout=300)
            except CliError as exc:
                if getattr(exc, "status", None) == 409:   # 断点位置不一致：按服务端说的续传
                    expected = exc.headers.get("X-Expected-Offset")  # type: ignore[attr-defined]
                    if expected is not None and int(expected) != offset:
                        offset = int(expected)
                        continue
                raise
            offset += len(block)
            on_progress(name, min(offset, total), total)
            if res.get("complete"):
                if res.get("sha256") != digest:
                    raise CliError(f"{name}：服务端摘要与本地不一致")
                return res
            if not block and total:
                raise CliError(f"{name}：上传提前结束")


def send_files(server: str, key: str, paths: list[str], expire_hours: float = 24,
               label: str = "", public_url: str = "", on_progress=progress,
               max_downloads: int = 1) -> dict:
    """经常驻服务的一次性传输：建传输 → 上传 → 给出文件码。返回 {code, grant_id, ...}。

    传输到期 / 取够次数 / 被撤销后，服务端会自动连文件一起清理，不会留下空间与授权。
    """
    cli = Client(server)
    cli.login(key)
    label = (label or ", ".join(os.path.basename(p) for p in paths))[:60]
    tr = cli.request("POST", "/api/transfers", {
        "label": label, "expires_hours": expire_hours, "max_downloads": max_downloads})["transfer"]
    try:
        for p in paths:
            upload_file(cli, tr["space_id"], p, on_progress)
    except BaseException:
        try:                                    # 半途失败：撤销，交给服务端清理
            cli.request("POST", "/api/grants/revoke", {"id": tr["id"]})
        except CliError:
            pass
        raise
    code = make_code(normalize_base(public_url or _public_url(cli) or cli.base), tr["secret"])
    return {
        "code": code, "grant_id": tr["id"], "count": len(paths),
        "bytes": sum(os.path.getsize(p) for p in paths), "expire_hours": expire_hours,
        "max_downloads": max_downloads, "command": f"{cmd_prefix()} get {code}",
    }


def cmd_send_via_server(args) -> int:
    paths = expand_paths(args.files) if args.files else expand_paths(pick_interactively())
    total_bytes = sum(os.path.getsize(p) for p in paths)
    print(f"上传 {len(paths)} 个文件（{human(total_bytes)}）到 {normalize_base(args.server)} …",
          file=sys.stderr)

    last = {"name": None}

    def show(name, done, total):
        if last["name"] not in (None, name):
            progress_end()
        last["name"] = name
        progress(name, done, total)

    try:
        res = send_files(args.server, find_admin_key(args), paths, args.expire, args.label,
                         args.public_url or "", show, args.max_downloads)
    except KeyboardInterrupt:
        print("\n发送中断，传输空间里可能残留已上传的文件", file=sys.stderr)
        return 130
    progress_end()
    print()
    print(f"文件码:  {res['code']}")
    print(f"取件命令: {res['command']}")
    print("          （加 -o <目录> 指定保存位置，默认保存到运行命令时所在目录）")
    exp = f"{args.expire:g} 小时后过期" if args.expire else "永不过期"
    times = f"可取 {args.max_downloads} 次" if args.max_downloads else "取件次数不限"
    print(f"共 {res['count']} 个文件，{human(res['bytes'])}，{exp}，{times}；"
          f"到期/取完后服务端自动清理；撤销：{cmd_prefix()} revoke {res['grant_id']}")
    return 0


def _public_url(cli: Client) -> str:
    """服务端知道自己的对外地址（可能是局域网 IP），比 127.0.0.1 更适合发给别人。"""
    try:
        return cli.request("GET", "/api/spaces").get("public_url") or ""
    except CliError:
        return ""


def cmd_send(args) -> int:
    """默认直连：本机临时监听，等对方 get；加 --server 才走常驻服务。"""
    if args.server:
        return cmd_send_via_server(args)
    paths = expand_paths(args.files or pick_interactively(), allow_dirs=True)
    note = lambda m: print(f"  · {m}", file=sys.stderr)  # noqa: E731
    sender = DirectSender(paths, port=args.port, receivers=args.receivers, on_event=note)
    print(f"准备 {len(sender.entries)} 个文件（{human(sender.total_bytes)}），计算校验值…",
          file=sys.stderr)
    sender.prepare_hashes()
    sender.start()
    ips = [args.ip] if args.ip else lan_addresses()
    code = make_code(f"http://{ips[0]}:{sender.port}", sender.token)
    print()
    print(f"文件码:  {code}")
    print(f"取件命令: {cmd_prefix()} get {code}")
    print("          （加 -o <目录> 指定保存位置，默认保存到运行命令时所在目录）")
    if len(ips) > 1 and not args.ip:
        print("          其他可用地址（若上面的对方连不上，换一个 IP 再试）：" +
              "  ".join(f"{ip}:{sender.port}" for ip in ips[1:]))
    print(f"等待接收端连接… 一次性，{args.timeout:g} 分钟内无人取件将自动退出；Ctrl-C 取消。")
    print("提示：首次监听时系统防火墙可能弹窗，请选择「允许访问」；传输为明文 HTTP，仅限可信局域网。",
          file=sys.stderr)
    try:
        reason = sender.wait(args.timeout * 60)
    except KeyboardInterrupt:
        sender.stop("cancelled")
        reason = "cancelled"
    messages = {"done": "已送达，发送端退出", "timeout": "超时无人取件，已退出",
                "failures": "错误尝试过多，已退出", "cancelled": "已取消"}
    print(messages.get(reason, reason), file=sys.stderr)
    return {"done": 0, "cancelled": 130}.get(reason, 1)


# ------------------------------------------------------------------ 直连发送（去中心化）
def lan_addresses() -> list[str]:
    """本机可能被局域网访问到的 IPv4 地址，首选地址排第一（装了 docker/VPN 时可能不止一个）。"""
    found: list[str] = []
    try:                       # 不真正发包：只让系统选出默认路由的出口地址
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
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
        self.entries = self._collect(paths)
        if not self.entries:
            raise CliError("没有可发送的文件")
        self.finished = threading.Event()
        self.reason = ""
        self._lock = threading.Lock()
        self._sessions: set[str] = set()
        self._failures = 0
        self._done = 0
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
        out = []
        for full, rel in entries:
            if rel not in seen:
                seen.add(rel)
                out.append({"id": str(len(out)), "path": full, "name": rel,
                            "size": os.path.getsize(full)})
        return out

    def prepare_hashes(self, on_progress=None):
        for e in self.entries:
            e["sha256"] = sha256_file(e["path"])
            if on_progress:
                on_progress(e["name"])

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

            def _authed(self):
                jar = self.headers.get("Cookie") or ""
                m = re.search(r"ld_direct=([A-Za-z0-9_-]+)", jar)
                return bool(m) and m.group(1) in sender._sessions

            def do_POST(self):
                path = urllib.parse.urlparse(self.path).path
                if path == "/api/login":
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
                    if not self._authed():
                        return self._json({"ok": False, "error": "未登录"}, 401)
                    with sender._lock:
                        sender._done += 1
                        complete = sender._done >= sender.receivers
                    self._json({"ok": True})
                    sender.on_event("接收完成")
                    if complete:
                        sender.stop("done")
                    return
                self._json({"ok": False, "error": "not found"}, 404)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if not self._authed():
                    return self._json({"ok": False, "error": "未登录"}, 401)
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
                        sender.on_event(f"已发送：{entry['name']}  ({human(size)})")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler


# ------------------------------------------------------------------ 接收
def unique_path(directory: str, name: str) -> str:
    stem, ext = os.path.splitext(name)
    candidate, i = os.path.join(directory, name), 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem} ({i}){ext}")
        i += 1
    return candidate


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def safe_local_name(name: str) -> str:
    """服务端给的名字不可信：去路径、去 Windows 非法字符/保留名，三个平台都能落盘。"""
    name = name.replace("\\", "/").split("/")[-1]   # 不用 os.path.basename：Windows 上会把 "a:b" 当盘符
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f")
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip().rstrip(". ")
    if not name:
        name = "download"
    if name.split(".")[0].upper() in _WIN_RESERVED:
        name = "_" + name
    return name


def safe_relpath(rel: str) -> str:
    """把对端给的相对路径清洗成本地安全路径：逐段清洗，拒绝 ..、空段与绝对路径。"""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise CliError(f"不安全的路径：{rel!r}")
    return os.path.join(*[safe_local_name(p) for p in parts])


def download_file(cli: Client, item: dict, directory: str, force: bool,
                  on_progress=progress, _retry=True) -> str:
    rel = safe_relpath(item.get("path") or item["name"])
    name = os.path.basename(rel)
    target = os.path.join(directory, rel) if force else unique_path(directory, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = target + ".part"

    resume = os.path.getsize(part) if os.path.exists(part) else 0
    size = int(item.get("size") or 0)
    if resume and resume >= size:          # 残留分块不可信：从头来
        resume = 0
    headers = {"Range": f"bytes={resume}-"} if resume else {}
    resp = cli.request("GET", "/api/download?" + urllib.parse.urlencode({"id": item["id"]}),
                       headers=headers, timeout=300, raw=True)
    if resume and resp.status != 206:      # 对端不支持续传
        resume = 0
    expect = (resp.headers.get("X-File-SHA256") or item.get("sha256") or "").lower()
    total = size or (int(resp.headers.get("Content-Length") or 0) + resume)
    h = hashlib.sha256()
    if resume:                              # 续传：先把已有部分并入摘要
        with open(part, "rb") as old:
            for block in iter(lambda: old.read(1024 * 1024), b""):
                h.update(block)
    done = resume
    with resp, open(part, "ab" if resume else "wb") as f:
        while True:
            block = resp.read(1024 * 1024)
            if not block:
                break
            f.write(block)
            h.update(block)
            done += len(block)
            on_progress(name, done, total)
    # 网络中断：保留 .part，下次 get 会从断点继续
    if total and done != total:
        raise CliError(f"{name}：下载不完整（{done}/{total}），重新运行 get 可续传")
    if expect and h.hexdigest() != expect:
        try:
            os.remove(part)
        except OSError:
            pass
        if resume and _retry:               # 续传拼出的内容不对：整份重下一次
            return download_file(cli, item, directory, force, on_progress, _retry=False)
        raise CliError(f"{name}：SHA-256 校验失败，已丢弃")
    os.replace(part, target)
    return target


def list_remote(code: str, only=None) -> tuple[Client, list[dict]]:
    base, token = parse_code(code)
    cli = Client(base)
    cli.login(token)
    files = cli.request("GET", "/api/files").get("files", [])
    if only:
        wanted = set(only)
        files = [f for f in files if f["name"] in wanted]
    if not files:
        raise CliError("没有可下载的文件（文件码已过期、被撤销或没有匹配）")
    return cli, files


def fetch_files(code: str, directory: str, only=None, force: bool = False,
                on_progress=progress, on_done=None) -> list[str]:
    """按文件码把文件下载到 directory，逐个校验 SHA-256；返回落盘路径列表。"""
    cli, files = list_remote(code, only)
    directory = os.path.abspath(os.path.expanduser(directory or "."))
    os.makedirs(directory, exist_ok=True)
    saved = []
    for item in files:
        target = download_file(cli, item, directory, force, on_progress)
        saved.append(target)
        if on_done:
            on_done(item, target)
    try:                                    # 直连发送端据此结束；中心化服务没有该接口，忽略
        cli.request("POST", "/api/done", {})
    except CliError:
        pass
    return saved


def cmd_get(args) -> int:
    if args.list:
        cli, files = list_remote(args.code, args.only)
        print(f"{cli.base} 上共 {len(files)} 个文件：", file=sys.stderr)
        for f in files:
            print(f"{f['name']}  {human(f['size'])}  sha256={f['sha256'][:12]}…")
        return 0
    directory = os.path.abspath(os.path.expanduser(args.output or "."))
    print(f"→ {directory}", file=sys.stderr)
    last = {"name": None}

    def show(name, done, total):
        if last["name"] not in (None, name):
            progress_end()
        last["name"] = name
        progress(name, done, total)

    def done(item, target):
        progress_end()
        last["name"] = None
        print(f"✓ {target}  ({human(item['size'])}, 已校验 SHA-256)")

    fetch_files(args.code, directory, args.only, args.force, show, done)
    return 0


# ------------------------------------------------------------------ 撤销
def cmd_revoke(args) -> int:
    cli = Client(args.server)
    cli.login(find_admin_key(args))
    cli.request("POST", "/api/grants/revoke", {"id": args.grant_id})
    print(f"已撤销 {args.grant_id}，文件码立即失效")
    return 0


def cmd_serve(args) -> int:
    """启动服务（Linux 上没有图形启动器，直接用它）。参数原样交给 server.py。"""
    try:
        import server as srv
    except ImportError:
        raise CliError("找不到 server.py：serve 需要与 server.py / store.py / static 放在一起") from None
    return srv.main(args.rest) or 0


# ------------------------------------------------------------------ 入口
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="landrop.py", description="LAN Drop 命令行：文件码快传")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="直连发送：生成文件码并等待对方 get（无需服务）；--server 则上传到常驻服务")
    s.add_argument("files", nargs="*", help="文件或目录（可用通配符）；省略则交互选择")
    s.add_argument("--ip", help="直连模式：写进文件码的本机地址（默认自动探测；多网卡/VPN 时指定）")
    s.add_argument("--port", type=int, default=0, help="直连模式：监听端口（默认随机）")
    s.add_argument("--receivers", type=int, default=1, metavar="N", help="直连模式：允许 N 个接收者取完后退出（默认 1）")
    s.add_argument("--timeout", type=float, default=30, metavar="分钟", help="直连模式：无人取件的等待时长（默认 30）")
    s.add_argument("--server", default=os.environ.get("LANDROP_URL"),
                   help="改为上传到常驻服务（地址如 http://192.168.1.5:8000，或环境变量 LANDROP_URL）")
    s.add_argument("--public-url", help="--server 模式：写进文件码里的对外地址")
    s.add_argument("--key", help="管理员密钥（或环境变量 LANDROP_ADMIN_KEY）")
    s.add_argument("--data-dir", help="数据目录，从中读取 admin-key.txt（默认 ./data，其次平台默认目录）")
    s.add_argument("--expire", type=float, default=24, metavar="小时",
                   help="--server 模式：文件码有效小时数，0 表示永不过期（默认 24）")
    s.add_argument("--max-downloads", type=int, default=1, metavar="N",
                   help="--server 模式：最多被完整取件几次，0 表示不限（默认 1，即一次性）")
    s.add_argument("--label", help="这批文件的备注（默认用文件名）")
    s.set_defaults(fn=cmd_send)

    g = sub.add_parser("get", help="用文件码把文件下载到当前目录（或 -o 指定目录）")
    g.add_argument("code", help="文件码，形如 192.168.1.5:8000/令牌")
    g.add_argument("-o", "--output", metavar="目录", help="保存目录（默认当前目录，不存在会创建）")
    g.add_argument("--only", nargs="+", metavar="文件名", help="只下载指定文件名")
    g.add_argument("--list", action="store_true", help="只列出文件，不下载")
    g.add_argument("-f", "--force", action="store_true", help="同名文件直接覆盖（默认自动改名）")
    g.set_defaults(fn=cmd_get)

    v = sub.add_parser("serve", help="启动 LAN Drop 服务（参数同 server.py，如 --data-dir ./data）",
                       add_help=False)
    v.add_argument("rest", nargs=argparse.REMAINDER)
    v.set_defaults(fn=cmd_serve)

    r = sub.add_parser("revoke", help="撤销一个文件码（需要管理员密钥）")
    r.add_argument("grant_id", help="send 结束时打印的授权 ID（gs_…）")
    r.add_argument("--server", default=os.environ.get("LANDROP_URL", DEFAULT_URL))
    r.add_argument("--key")
    r.add_argument("--data-dir")
    r.set_defaults(fn=cmd_revoke)
    return p


def main(argv=None) -> int:
    setup_console()
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "serve":     # 参数原样交给 server.py，避免被本解析器截走
        try:
            return cmd_serve(argparse.Namespace(rest=argv[1:]))
        except CliError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except CliError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
