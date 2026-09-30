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


def expand_paths(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        item = os.path.expanduser(item)
        matches = sorted(glob.glob(item)) if any(c in item for c in "*?[") else [item]
        if not matches:
            raise CliError(f"没有匹配：{item}")
        for m in matches:
            if os.path.isdir(m):
                raise CliError(f"{m} 是目录；请先打包（如 tar/zip）或用通配符选文件")
            if not os.path.isfile(m):
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
               label: str = "", public_url: str = "", on_progress=progress) -> dict:
    """上传文件并签发取件授权。返回 {code, grant_id, count, bytes, expire_hours, command}。

    命令行与图形界面共用；进度通过回调交出，调用方决定怎么显示。
    """
    cli = Client(server)
    cli.login(key)
    label = (label or ", ".join(os.path.basename(p) for p in paths))[:60]
    space = cli.request("POST", "/api/spaces", {"name": f"传输 {time.strftime('%m-%d %H:%M:%S')}"})
    space_id = space["space"]["id"]
    for p in paths:
        upload_file(cli, space_id, p, on_progress)
    created = cli.request("POST", "/api/grants", {
        "kind": "share", "space_id": space_id, "perm": "delete_own", "mode": "token",
        "label": label, "expires_days": (expire_hours / 24) if expire_hours else 0})
    grant = created["grant"]
    token = grant.get("secret") or ""
    if not token:
        raise CliError("服务端没有返回令牌（grant 响应里无 secret）")
    code = make_code(normalize_base(public_url or _public_url(cli) or cli.base), token)
    return {
        "code": code, "grant_id": grant["id"], "count": len(paths),
        "bytes": sum(os.path.getsize(p) for p in paths), "expire_hours": expire_hours,
        "command": f"{cmd_prefix()} get {code}",
    }


def cmd_send(args) -> int:
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
                         args.public_url or "", show)
    except KeyboardInterrupt:
        print("\n发送中断，传输空间里可能残留已上传的文件", file=sys.stderr)
        return 130
    progress_end()
    print()
    print(f"文件码:  {res['code']}")
    print(f"取件命令: {res['command']}")
    print("          （加 -o <目录> 指定保存位置，默认保存到运行命令时所在目录）")
    exp = f"{args.expire:g} 小时后过期" if args.expire else "永不过期"
    print(f"共 {res['count']} 个文件，{human(res['bytes'])}，{exp}；"
          f"撤销：{cmd_prefix()} revoke {res['grant_id']}")
    return 0


def _public_url(cli: Client) -> str:
    """服务端知道自己的对外地址（可能是局域网 IP），比 127.0.0.1 更适合发给别人。"""
    try:
        return cli.request("GET", "/api/spaces").get("public_url") or ""
    except CliError:
        return ""


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


def download_file(cli: Client, item: dict, directory: str, force: bool,
                  on_progress=progress) -> str:
    name = safe_local_name(item["name"])
    target = os.path.join(directory, name) if force else unique_path(directory, name)
    part = target + ".part"
    resp = cli.request("GET", "/api/download?" + urllib.parse.urlencode({"id": item["id"]}),
                       timeout=300, raw=True)
    expect = (resp.headers.get("X-File-SHA256") or item.get("sha256") or "").lower()
    total = int(resp.headers.get("Content-Length") or item.get("size") or 0)
    h, done = hashlib.sha256(), 0
    try:
        with resp, open(part, "wb") as f:
            while True:
                block = resp.read(1024 * 1024)
                if not block:
                    break
                f.write(block)
                h.update(block)
                done += len(block)
                on_progress(name, done, total)
        if total and done != total:
            raise CliError(f"{name}：下载不完整（{done}/{total}）")
        if expect and h.hexdigest() != expect:
            raise CliError(f"{name}：SHA-256 校验失败，已丢弃")
        os.replace(part, target)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
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

    s = sub.add_parser("send", help="上传一个或多个文件，生成文件码与取件命令")
    s.add_argument("files", nargs="*", help="文件路径（可用通配符）；省略则交互选择")
    s.add_argument("--server", default=os.environ.get("LANDROP_URL", DEFAULT_URL),
                   help=f"服务地址（默认 {DEFAULT_URL}，或环境变量 LANDROP_URL）")
    s.add_argument("--public-url", help="写进文件码里的对外地址（默认取服务端探测到的局域网地址）")
    s.add_argument("--key", help="管理员密钥（或环境变量 LANDROP_ADMIN_KEY）")
    s.add_argument("--data-dir", help="数据目录，从中读取 admin-key.txt（默认 ./data，其次平台默认目录）")
    s.add_argument("--expire", type=float, default=24, metavar="小时",
                   help="文件码有效小时数，0 表示永不过期（默认 24）")
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
