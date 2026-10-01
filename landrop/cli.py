#!/usr/bin/env python3
"""LAN Drop 命令行。

    landrop send a.zip dir/        直连发送：生成文件码并等待对方取件（无需服务）
    landrop get <文件码> [-o 目录]  用文件码取件
    landrop serve [--data-dir …]   在本机启动收集服务（网页 + CLI）
    landrop send --server URL …    上传到常驻服务，生成一次性取件码
    landrop revoke <ID>            撤销一个传输/分享
    landrop gui                    打开桌面窗口（与安装包里的 LAN Drop 是同一个程序）
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

from . import __version__
from .common import (CliError, collect_entries, human, make_code, normalize_base, progress,
                     progress_end, setup_console, cmd_prefix)
from .direct import DirectSender, lan_addresses
from .remote import (Client, fetch_files, list_remote, read_admin_key, send_files)

DEFAULT_URL = "http://127.0.0.1:8000"


def find_admin_key(args) -> str:
    if args.key:
        return args.key
    if os.environ.get("LANDROP_ADMIN_KEY"):
        return os.environ["LANDROP_ADMIN_KEY"]
    return read_admin_key(args.data_dir)


def pick_interactively() -> list[str]:
    if not sys.stdin.isatty():
        raise CliError(f"没有指定文件。用法：{cmd_prefix()} send 文件或文件夹 [...]")
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
            if not (os.path.isfile(m) or os.path.isdir(m)):
                raise CliError(f"文件不存在：{m}")
            ap = os.path.abspath(m)
            if ap not in out:
                out.append(ap)
    return out


def print_code(code: str, qr: bool = False):
    """send 的输出：文件码、取件命令、浏览器网址（窗口里的发送页显示的是同样三样）。"""
    url = code if code.startswith(("http://", "https://")) else "http://" + code
    print(f"文件码:  {code}")
    print(f"取件命令: {cmd_prefix()} get {code}")
    print("          （加 -o <目录> 指定保存位置，默认保存到运行命令时所在目录）")
    print(f"浏览器:  {url}   （手机 / 没装 LAN Drop 的设备直接打开即可下载）")
    if qr:
        from .qr import encode, to_terminal
        print()
        print(to_terminal(encode(url)))
        print("          手机扫码下载")


def cmd_send_via_server(args) -> int:
    paths = expand_paths(args.files or pick_interactively())
    entries = collect_entries(paths)
    total_bytes = sum(os.path.getsize(full) for full, _ in entries)
    print(f"上传 {len(entries)} 个文件（{human(total_bytes)}）到 {normalize_base(args.server)} …",
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
    print_code(res["code"], args.qr)
    exp = f"{args.expire:g} 小时后过期" if args.expire else "永不过期"
    times = f"可取 {args.max_downloads} 次" if args.max_downloads else "取件次数不限"
    print(f"共 {res['count']} 个文件，{human(res['bytes'])}，{exp}，{times}；"
          f"到期/取完后服务端自动清理；撤销：{cmd_prefix()} revoke {res['grant_id']}")
    return 0


def cmd_send(args) -> int:
    """默认直连：本机临时监听，等对方 get；加 --server 才走常驻服务。"""
    if args.server:
        return cmd_send_via_server(args)
    paths = expand_paths(args.files or pick_interactively())
    note = lambda m: print(f"  · {m}", file=sys.stderr)  # noqa: E731
    sender = DirectSender(paths, port=args.port, receivers=args.receivers, on_event=note)
    print(f"准备 {len(sender.entries)} 个文件（{human(sender.total_bytes)}），计算校验值…",
          file=sys.stderr)
    sender.prepare_hashes()
    sender.start()
    ips = [args.ip] if args.ip else lan_addresses()
    code = make_code(f"http://{ips[0]}:{sender.port}", sender.token)
    print()
    print_code(code, args.qr)
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


def cmd_revoke(args) -> int:
    cli = Client(args.server)
    cli.login(find_admin_key(args))
    cli.request("POST", "/api/grants/revoke", {"id": args.grant_id})
    print(f"已撤销 {args.grant_id}，文件码立即失效")
    return 0


def cmd_serve(rest: list[str]) -> int:
    """启动收集服务：参数原样交给服务端（--data-dir / --port / --dir / --import …）。"""
    from .server import app
    return app.main(rest) or 0


def cmd_gui(args) -> int:
    try:
        from .gui import launcher
    except ImportError as exc:
        raise CliError(f"图形界面不可用（需要带 tkinter 的 Python）：{exc}") from None
    return launcher.main(["--autostart"] if args.autostart else []) or 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="landrop", description="LAN Drop：局域网文件互传与收集")
    p.add_argument("--version", action="version", version=f"landrop {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="命令")

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
    s.add_argument("--qr", action="store_true", help="在终端里显示二维码（手机扫码用浏览器下载）")
    s.set_defaults(fn=cmd_send)

    g = sub.add_parser("get", help="用文件码把文件下载到当前目录（或 -o 指定目录）")
    g.add_argument("code", help="文件码，形如 192.168.1.5:8000/令牌")
    g.add_argument("-o", "--output", metavar="目录", help="保存目录（默认当前目录，不存在会创建）")
    g.add_argument("--only", nargs="+", metavar="文件名", help="只下载指定文件名")
    g.add_argument("--list", action="store_true", help="只列出文件，不下载")
    g.add_argument("-f", "--force", action="store_true", help="同名文件直接覆盖（默认自动改名）")
    g.set_defaults(fn=cmd_get)

    sub.add_parser("serve", help="在本机启动收集服务（网页 + CLI；参数见 landrop serve --help）",
                   add_help=False)

    gui = sub.add_parser("gui", help="打开桌面窗口")
    gui.add_argument("--autostart", action="store_true", help="打开后立即启动收集服务")
    gui.set_defaults(fn=cmd_gui)

    r = sub.add_parser("revoke", help="撤销一个文件码（需要管理员密钥）")
    r.add_argument("grant_id", help="send 结束时打印的授权 ID（gs_…）")
    r.add_argument("--server", default=os.environ.get("LANDROP_URL", DEFAULT_URL))
    r.add_argument("--key")
    r.add_argument("--data-dir")
    r.set_defaults(fn=cmd_revoke)
    return p


def _pause_if_double_clicked():
    """Windows 上在资源管理器里双击 landrop.exe：控制台窗口一闪而过。只有本进程用这个控制台时停一下。"""
    if not sys.platform.startswith("win") or not sys.stdin or not sys.stdin.isatty():
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleProcessList((ctypes.c_uint * 2)(), 2) <= 1:
            input("这是命令行工具，请在终端里使用；想要图形界面请打开 LAN Drop。按回车键关闭…")
    except Exception:  # noqa: BLE001
        pass


def main(argv=None) -> int:
    setup_console()
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "serve":     # 参数原样交给服务端，避免被本解析器截走
        try:
            return cmd_serve(argv[1:])
        except CliError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):        # 不带命令：给出用法，而不是一行报错
        parser.print_help()
        print(f"\n例：{cmd_prefix()} send a.zip    {cmd_prefix()} get <文件码>    {cmd_prefix()} gui")
        _pause_if_double_clicked()
        return 0
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
