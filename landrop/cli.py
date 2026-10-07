"""Local share management and the same six-digit receive/collection protocol as the web UI."""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import signal
import sys
import time

from . import __version__
from . import common, remote
from .server.app import Runtime
from .server.store import Store, TEXT_LIMIT


def expand_paths(items):
    result = []
    for item in items:
        item = os.path.expanduser(item)
        matches = sorted(glob.glob(item)) if not os.path.exists(item) and any(ch in item for ch in "*?[") else [item]
        if not matches:
            raise common.CliError(f"没有匹配：{item}")
        for path in matches:
            if not os.path.isfile(path) and not os.path.isdir(path):
                raise common.CliError(f"文件或目录不存在：{path}")
            path = os.path.abspath(path)
            if path not in result:
                result.append(path)
    return result


def pick_files():
    if not sys.stdin.isatty():
        raise common.CliError("请选择文件，或使用 --upload 创建收集、--text 发布文本")
    choices = sorted(path for path in os.listdir(".") if os.path.isfile(path) or os.path.isdir(path))
    for index, name in enumerate(choices, 1):
        print(f"{index:>3}. {name}")
    value = input("选择编号（空格分隔，a=全部）：").strip()
    if value.lower() == "a":
        return choices
    try:
        result = [choices[int(part) - 1] for part in value.split() if 1 <= int(part) <= len(choices)]
    except (ValueError, IndexError):
        raise common.CliError("编号不正确") from None
    if not result:
        raise common.CliError("没有选择文件")
    return result


def read_text(args):
    if args.text_file:
        with open(os.path.expanduser(args.text_file), "rb") as stream:
            raw = stream.read(TEXT_LIMIT + 1)
    elif args.text == "-":
        raw = sys.stdin.buffer.read(TEXT_LIMIT + 1)
    elif args.text is not None:
        return args.text
    else:
        return None
    if len(raw) > TEXT_LIMIT:
        raise common.CliError("单条文本上限为 10 MiB")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise common.CliError("文本须为 UTF-8 编码") from None


def share_options(parser, editing=False):
    parser.add_argument("--label", help="分享名称")
    parser.add_argument("--expire", type=float, help="有效小时数，0=长期")
    parser.add_argument("--dir", dest="collection_base", help="收集目录，任务子目录自动创建")
    parser.add_argument("--confirm-overlap", action="store_true", help="确认整目录分享可能公开私密收集内容")
    for name, help_text in (("upload", "允许上传文件和提交文本"), ("public", "公开收到的文件和文本")):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--" + name, action="store_true", default=None, help=help_text)
        if editing:
            group.add_argument("--no-" + name, dest=name, action="store_false")
    text = parser.add_mutually_exclusive_group()
    text.add_argument("--text", help="发布文本，- 从标准输入读取")
    text.add_argument("--text-file", help="读取 UTF-8 文本文件")


def build_parser():
    parser = argparse.ArgumentParser(prog="landrop", description="LAN Drop：本机分享、设备互传与收集")
    parser.add_argument("--version", action="version", version=f"landrop {__version__}")
    parser.add_argument("--data-dir", help="本机状态目录，也可用 LANDROP_DATA_DIR")
    commands = parser.add_subparsers(dest="command")
    send = commands.add_parser("send", help="创建分享，必要时启动本机服务")
    send.add_argument("files", nargs="*")
    send.add_argument("--qr", action="store_true")
    share_options(send)
    shares = commands.add_parser("shares", help="在本机查看、修改、停用或删除分享")
    shares.add_argument("id", nargs="?")
    shares.add_argument("--files", nargs="*", help="替换共享文件列表；留空清空列表")
    shares.add_argument("--json", action="store_true")
    shares.add_argument("--yes", action="store_true", help="确认删除分享或清空文本")
    actions = shares.add_mutually_exclusive_group()
    for action in ("enable", "disable", "delete", "rotate", "clear-text"):
        actions.add_argument("--" + action, action="store_true")
    share_options(shares, editing=True)
    get = commands.add_parser("get", help="用地址和授权码或便捷链接接收文件")
    get.add_argument("address")
    get.add_argument("--code", default="")
    get.add_argument("-o", "--output", default=".")
    get.add_argument("--list", action="store_true")
    get.add_argument("--only", nargs="+")
    get.add_argument("-f", "--force", action="store_true", help="明确覆盖同名文件")
    put = commands.add_parser("put", help="向允许上传的分享投递文件或文本")
    put.add_argument("address")
    put.add_argument("files", nargs="*")
    put.add_argument("--code", default="")
    text = put.add_mutually_exclusive_group()
    text.add_argument("--text")
    text.add_argument("--text-file")
    serve = commands.add_parser("serve", help="启动本机服务，Ctrl-C 停止，不自动撤销分享")
    serve.add_argument("--port", type=int)
    serve.add_argument("--ip", help="访问地址，默认自动识别")
    serve.add_argument("--collect-dir", help="设置默认收集目录（容器使用持久卷内目录）")
    serve.add_argument("--open-browser", action="store_true")
    commands.add_parser("stop", help="停止同一状态目录下的本机服务")
    settings = commands.add_parser("settings", help="查看或调整本机设置")
    settings.add_argument("--port", type=int)
    settings.add_argument("--ip", help="网卡地址，auto=自动")
    settings.add_argument("--collect-dir")
    settings.add_argument("--recv-dir")
    settings.add_argument("--text-preview-mib", type=int)
    settings.add_argument("--image-preview-mib", type=int)
    settings.add_argument("--theme", choices=("system", "light", "dark"))
    settings.add_argument("--clear-history", action="store_true")
    commands.add_parser("gui", help="打开三页桌面窗口，不自动启动服务")
    for command in commands.choices.values():
        command.add_argument("--data-dir", default=argparse.SUPPRESS)
    return parser


def _options(args):
    values = {"label": args.label, "allow_upload": args.upload, "public_received": args.public,
              "collection_base": args.collection_base, "confirm_overlap": args.confirm_overlap}
    if args.expire is not None:
        if not math.isfinite(args.expire) or args.expire < 0:
            raise common.CliError("有效期须为非负小时数")
        values["expires_at"] = time.time() + args.expire * 3600 if args.expire else None
    return values


def _print_share(store, runtime, share, qr=False):
    info = runtime.info
    ip = store.settings()["ip"] or common.lan_addresses()[0]
    ip = f"[{ip}]" if ":" in ip else ip
    base = info["base"] if info else f"http://{ip}:{store.settings()['port']}"
    print(f"分享: {share['label']}\nID: {share['id']}\n访问地址: {base}\n授权码: {share['code']}")
    print(f"便捷链接: {common.make_link(base, share['code'])}")
    if not info:
        print("服务未启动，运行 landrop serve 后可访问")
    if qr:
        from .qr import encode, to_terminal
        print(to_terminal(encode(common.make_link(base, share["code"]))))


def _confirm(args):
    if args.yes:
        return
    if not sys.stdin.isatty() or input("将移除分享配置/文本，不删除磁盘文件。确认？[y/N] ").lower() != "y":
        raise common.CliError("未确认；自动化操作请明确添加 --yes")


def _wait(runtime):
    def interrupt(_signal, _frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        while runtime.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        runtime.stop()
        print("\n服务已停止，分享配置与文件保留")
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv=None):
    common.setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "gui":
        if args.data_dir:
            os.environ["LANDROP_DATA_DIR"] = common.resolve_data_dir(args.data_dir)[0]
        from .gui.launcher import main as gui_main
        return gui_main([])
    store = runtime = None
    try:
        store = Store(common.resolve_data_dir(args.data_dir)[0])
        runtime = Runtime(store)
        if args.command == "send":
            text = read_text(args)
            paths = args.files or ([] if args.upload or text is not None else pick_files())
            share = store.save_share(paths=expand_paths(paths), text=text, **_options(args))
            info = runtime.start()
            _print_share(store, runtime, share, args.qr)
            if runtime.httpd:
                print("本机服务持续运行；Ctrl-C 停止服务，不删除分享或文件。")
                _wait(runtime)
        elif args.command == "serve":
            if args.collect_dir:
                store.configure(collect_dir=args.collect_dir)
            runtime.start(args.port, args.ip)
            print(f"访问地址: {runtime.info['base']}")
            print("Ctrl-C 停止服务，已有分享和文件保留。")
            if args.open_browser:
                import webbrowser
                webbrowser.open(runtime.info["local_url"])
            if runtime.httpd:
                _wait(runtime)
        elif args.command == "stop":
            runtime.stop()
            print("本机服务已停止，分享和文件保留")
        elif args.command == "shares":
            if not args.id:
                shares = store.shares()
                if args.json:
                    print(json.dumps(shares, ensure_ascii=False))
                else:
                    for share in shares:
                        print(f"{share['id']}  {share['code']}  {'启用' if share['active'] else '停用/过期'}  {share['label']}")
            elif args.delete or args.clear_text:
                _confirm(args)
                (store.delete_share if args.delete else store.clear_texts)(args.id)
                print("已处理，磁盘文件保留")
            else:
                if args.enable or args.disable:
                    share = store.set_enabled(args.id, args.enable)
                elif args.rotate:
                    share = store.rotate(args.id)
                elif any(value is not None for value in (args.files, args.label, args.upload, args.public,
                                                         args.expire, args.collection_base)):
                    share = store.save_share(args.id, paths=expand_paths(args.files) if args.files is not None else None,
                                             **_options(args))
                else:
                    share = store.share(args.id)
                text = read_text(args)
                if text is not None:
                    store.add_text(share["id"], text)
                _print_share(store, runtime, share)
        elif args.command == "get":
            if args.list:
                client, files = remote.list_remote(args.address, args.code, args.only, args.data_dir)
                for item in files:
                    print(f"{item['name']}  {common.human(item['size'])}")
                for item in client.content["texts"]:
                    print(f"文本 {item['id']}  {common.human(item['size'])}")
            else:
                directory = os.path.abspath(os.path.expanduser(args.output))
                saved = remote.fetch_files(args.address, directory, args.only, args.force,
                                           on_done=lambda item, target: print(f"已接收: {target}"),
                                           code=args.code, data_dir=args.data_dir)
                common.progress_end()
                store.record_receive(len(saved), directory)
        elif args.command == "put":
            client, _ = remote.list_remote(args.address, args.code, data_dir=args.data_dir)
            text = read_text(args)
            if not args.files and text is None:
                raise common.CliError("请选择文件或提供文本")
            for path, rel in common.collect_entries(expand_paths(args.files)):
                result = remote.upload_file(client, path, rel=rel)
                common.progress_end()
                print(f"已投递: {result['name']}")
            if text is not None:
                client.submit_text(text)
                print("文本已提交")
        elif args.command == "settings":
            values = {key: getattr(args, key) for key in
                      ("port", "ip", "collect_dir", "recv_dir", "text_preview_mib", "image_preview_mib", "theme")
                      if getattr(args, key) is not None}
            if runtime.running and any(key in values for key in ("port", "ip")):
                raise common.CliError("修改网络配置前请先运行 landrop stop，再手动启动服务")
            if values.get("ip") == "auto":
                values["ip"] = ""
            if args.clear_history:
                values["receive_history"] = []
            print(json.dumps(store.configure(**values) if values else store.settings(), ensure_ascii=False, indent=2))
        return 0
    except (common.CliError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130
    finally:
        if runtime:
            runtime.close()
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
