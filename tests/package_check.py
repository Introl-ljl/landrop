#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包产物的端到端验证（三个平台通用，CI 在「安装后」的位置上运行）。

    python tests/package_check.py --cli <landrop 可执行文件> [--gui <窗口可执行文件>]
                                  [--expect-data-dir <目录>]

检查：
  1. ``landrop --version`` 与源码版本一致；
  2. 直连：二进制 ``send`` → 二进制 ``get``，内容逐字节一致（含文件夹）；
  3. 收集服务：``serve`` 起得来、网页与管理面板能打开、``send --server`` / ``get`` 往返；
     给了 --expect-data-dir 时，不带 --data-dir 启动的服务必须把数据放在那里（免安装版 = 程序目录 data/）；
  4. 窗口：``--smoke-test`` 打开全部页面、启动并停止一次服务后正常退出（需要图形环境）。
"""
from __future__ import annotations

import argparse
import faulthandler
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

faulthandler.dump_traceback_later(420, exit=True)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from landrop import __version__  # noqa: E402

FAILED: list[str] = []
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")
ENV.pop("LANDROP_DATA_DIR", None)
ENV.pop("LANDROP_URL", None)
ENV.pop("LANDROP_ADMIN_KEY", None)


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        FAILED.append(msg)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(cmd, timeout=120, cwd=None):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd, env=ENV)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def spawn(cmd, cwd=None):
    """后台进程：stdout+stderr 合并，后台线程逐行读，避免管道写满把进程卡住。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd, env=ENV)
    lines: queue.Queue = queue.Queue()

    def pump():
        for raw in proc.stdout:
            lines.put(raw.decode("utf-8", "replace"))
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    return proc, lines


def wait_line(lines: queue.Queue, pattern: str, timeout=60):
    end = time.time() + timeout
    seen = []
    while time.time() < end:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            break
        seen.append(line)
        m = re.search(pattern, line)
        if m:
            return m, seen
    return None, seen


def wait_http(url: str, timeout=60) -> bytes | None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return None


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def make_payload(tmp: str) -> str:
    src = os.path.join(tmp, "src")
    os.makedirs(os.path.join(src, "folder", "子目录"))
    with open(os.path.join(src, "big.bin"), "wb") as f:
        f.write(os.urandom(3 * 1024 * 1024 + 17))
    with open(os.path.join(src, "folder", "a.txt"), "w", encoding="utf-8") as f:
        f.write("A")
    with open(os.path.join(src, "folder", "子目录", "中文.txt"), "w", encoding="utf-8") as f:
        f.write("内容")
    return src


def same(a: str, b: str) -> bool:
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


def check_version(cli):
    print("[版本]")
    rc, out, err = run([cli, "--version"])
    check(rc == 0 and out.strip() == f"landrop {__version__}", f"--version → {out.strip() or err.strip()}")


def check_direct(cli, tmp, src):
    print("[直连 send → get]")
    proc, lines = spawn([cli, "send", os.path.join(src, "big.bin"), os.path.join(src, "folder"),
                         "--ip", "127.0.0.1", "--timeout", "5"])
    m, seen = wait_line(lines, r"文件码:\s+(\S+)", 120)
    check(m is not None, "send 打印文件码" + ("" if m else "：" + "".join(seen)[-400:]))
    if not m:
        stop(proc)
        return
    out = os.path.join(tmp, "direct-out")
    rc, o, e = run([cli, "get", m.group(1), "-o", out], timeout=180)
    check(rc == 0, "get 成功" + ("" if rc == 0 else f"：{o}{e}"))
    check(os.path.isfile(os.path.join(out, "big.bin")) and
          same(os.path.join(src, "big.bin"), os.path.join(out, "big.bin")), "大文件逐字节一致")
    nested = os.path.join(out, "folder", "子目录", "中文.txt")
    check(os.path.isfile(nested), "文件夹结构与中文名保留")
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        pass
    check(proc.returncode == 0, f"发送端取完后自动退出（rc={proc.returncode}）")
    stop(proc)


def check_serve(cli, tmp, src, expect_data_dir):
    print("[收集服务 serve]")
    port = free_port()
    cwd = os.path.join(tmp, "cwd")
    os.makedirs(cwd, exist_ok=True)
    cmd = [cli, "serve", "--host", "127.0.0.1", "--port", str(port), "--admin-key", "pkg-check-key"]
    data_dir = expect_data_dir
    if not expect_data_dir:
        data_dir = os.path.join(tmp, "data")
        cmd += ["--data-dir", data_dir]
    proc, _lines = spawn(cmd, cwd=cwd)
    try:
        home = wait_http(f"http://127.0.0.1:{port}/", 90)
        check(home is not None and b"LAN Drop" in home, "首页可打开（网页资源已打包）")
        admin = wait_http(f"http://127.0.0.1:{port}/admin", 10)
        check(admin is not None and b"<html" in admin.lower(), "管理面板可打开")
        check(os.path.isfile(os.path.join(data_dir, "state.sqlite3")),
              f"数据目录位置正确：{data_dir}")
        check(not os.path.exists(os.path.join(cwd, "data")), "不会在当前目录乱建 data/")
        url = f"http://127.0.0.1:{port}"
        rc, o, e = run([cli, "send", "--server", url, "--key", "pkg-check-key",
                        os.path.join(src, "big.bin"), os.path.join(src, "folder")], timeout=180)
        m = re.search(r"文件码:\s+(\S+)", o)
        check(rc == 0 and m is not None, "send --server 上传并生成文件码" + ("" if m else f"：{o}{e}"))
        if m:
            out = os.path.join(tmp, "server-out")
            rc, o2, e2 = run([cli, "get", m.group(1), "-o", out], timeout=180)
            check(rc == 0 and same(os.path.join(src, "big.bin"), os.path.join(out, "big.bin")),
                  "get 取回内容一致" + ("" if rc == 0 else f"：{o2}{e2}"))
            check(os.path.isfile(os.path.join(out, "folder", "子目录", "中文.txt")),
                  "经服务器也保留文件夹结构")
            rc, o3, e3 = run([cli, "get", m.group(1), "-o", out + "2"], timeout=60)
            check(rc != 0, "一次性：第二次取件被拒绝")
    finally:
        stop(proc)


def check_gui(gui, tmp):
    print("[窗口 --smoke-test]")
    env_data = os.path.join(tmp, "gui-data")
    global ENV
    saved = ENV
    ENV = dict(ENV, LANDROP_DATA_DIR=env_data, LANDROP_PORT=str(free_port()))
    try:
        rc, out, err = run([gui, "--smoke-test"], timeout=180)
    finally:
        ENV = saved
    check(rc == 0, f"窗口自检通过（rc={rc}）" + ("" if rc == 0 else f"：{out}{err}"))
    check(os.path.isfile(os.path.join(env_data, "state.sqlite3")), "窗口内启动的服务写入了数据目录")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", required=True)
    ap.add_argument("--gui")
    ap.add_argument("--expect-data-dir")
    args = ap.parse_args()
    # 不带路径的名字（如 landrop）按 PATH 查找：仓库根目录下有同名的 landrop/ 包目录
    cli = shutil.which(args.cli) if os.sep not in args.cli and "/" not in args.cli \
        else (os.path.abspath(args.cli) if os.path.isfile(args.cli) else None)
    if not cli:
        print(f"找不到可执行文件：{args.cli}")
        return 1
    tmp = tempfile.mkdtemp(prefix="landrop-pkg-")
    src = make_payload(tmp)
    check_version(cli)
    check_direct(cli, tmp, src)
    check_serve(cli, tmp, src, args.expect_data_dir and os.path.abspath(args.expect_data_dir))
    if args.gui:
        gui = shutil.which(args.gui) if os.sep not in args.gui and "/" not in args.gui else args.gui
        check_gui(gui or args.gui, tmp)
    print(f"\n{'失败 ' + str(len(FAILED)) + ' 项' if FAILED else '全部通过'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
