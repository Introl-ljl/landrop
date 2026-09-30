#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""landrop.py 命令行端到端测试：真起服务 → send 多文件 → get 到指定目录 → 校验内容 → revoke。

用法: python3 tests/cli_check.py
"""
from __future__ import annotations

import contextlib
import io
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

import faulthandler
faulthandler.dump_traceback_later(240, exit=True)   # 卡死时打印各线程堆栈并退出，便于 CI 定位

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import landrop  # noqa: E402

KEY = "cli-test-key"
FAILED = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = landrop.main(argv)
    return rc, out.getvalue(), err.getvalue()


def start_direct_sender(args):
    """以子进程启动直连发送端；后台线程读 stdout，避免读管道把测试卡死。"""
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(ROOT, "landrop.py"), "send", *args, "--ip", "127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    lines: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout], daemon=True).start()
    code = ""
    end = time.time() + 20
    while time.time() < end and not code:
        try:
            m = re.search(r"文件码:\s+(\S+)", lines.get(timeout=0.5))
            code = m.group(1) if m else ""
        except queue.Empty:
            if proc.poll() is not None:
                break
    return proc, code


def direct_checks(tmp):
    print("\n[直连模式]")
    src = os.path.join(tmp, "dsrc")
    os.makedirs(os.path.join(src, "proj", "sub"))
    payload = os.urandom(400_000)
    with open(os.path.join(src, "big.bin"), "wb") as f:
        f.write(payload)
    with open(os.path.join(src, "proj", "a.txt"), "w") as f:
        f.write("A")
    with open(os.path.join(src, "proj", "sub", "b.txt"), "w") as f:
        f.write("B")

    proc, code = start_direct_sender([os.path.join(src, "big.bin"), os.path.join(src, "proj")])
    check(bool(code), f"直连 send 输出文件码 {code}")
    dest = os.path.join(tmp, "direct-out")
    os.makedirs(dest)
    with open(os.path.join(dest, "big.bin.part"), "wb") as f:      # 预置断点
        f.write(payload[:150_000])
    host, _, tail = code.partition("/")
    rc, _, _ = run(["get", host + "/wrong-token-xx", "-o", dest])
    check(rc == 1, "错误令牌被拒绝")
    rc, out, _ = run(["get", code, "-o", dest])
    check(rc == 0, "直连 get 成功")
    check(open(os.path.join(dest, "big.bin"), "rb").read() == payload, "断点续传后内容一致")
    check(open(os.path.join(dest, "proj", "sub", "b.txt")).read() == "B", "目录结构保留")
    try:
        check(proc.wait(timeout=15) == 0, "接收完成后发送端自动退出(0)")
    except subprocess.TimeoutExpired:
        proc.kill()
        check(False, "接收完成后发送端自动退出(0)")
    rc, _, _ = run(["get", code, "-o", dest])
    check(rc == 1, "一次性：发送端退出后文件码失效")

    # 错误次数过多 → 发送端自行关闭
    proc, code = start_direct_sender([os.path.join(src, "big.bin")])
    host = code.partition("/")[0]
    for _ in range(5):
        run(["get", host + "/nope", "-o", dest])
    try:
        check(proc.wait(timeout=15) == 1, "连续错误令牌后发送端关闭")
    except subprocess.TimeoutExpired:
        proc.kill()
        check(False, "连续错误令牌后发送端关闭")
    check(landrop.safe_relpath("a/b/c.txt") == os.path.join("a", "b", "c.txt"), "safe_relpath 正常路径")
    try:
        landrop.safe_relpath("../../etc/passwd")
        check(False, "safe_relpath 拒绝 ..")
    except landrop.CliError:
        check(True, "safe_relpath 拒绝 ..")


def main():
    tmp = tempfile.mkdtemp(prefix="landrop-cli-")
    port = free_port()
    server = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py"), "--data-dir", os.path.join(tmp, "data"),
         "--host", "127.0.0.1", "--port", str(port), "--admin-key", KEY, "--no-banner"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        src = os.path.join(tmp, "src")
        os.makedirs(src)
        files = {"a.bin": os.urandom(300_000), "中文 name.txt": "你好".encode(), "empty.dat": b""}
        for name, data in files.items():
            with open(os.path.join(src, name), "wb") as f:
                f.write(data)
        paths = [os.path.join(src, n) for n in files]

        rc, out, _ = run(["send", *paths, "--server", server, "--key", KEY, "--expire", "1",
                          "--public-url", server])
        check(rc == 0, "send 成功")
        m = re.search(r"文件码:\s+(\S+)", out)
        code = m.group(1) if m else ""
        check(bool(code) and code.startswith("127.0.0.1:"), f"输出文件码 {code}")
        check("get " + code in out, "输出可复制的取件命令")
        gid = re.search(r"revoke (gs_\w+)", out)

        dest = os.path.join(tmp, "新 目录")
        rc, out, _ = run(["get", code, "-o", dest])
        check(rc == 0, "get 成功")
        for name, data in files.items():
            p = os.path.join(dest, name)
            check(os.path.isfile(p) and open(p, "rb").read() == data, f"内容一致：{name}")

        rc, out, _ = run(["get", code, "-o", dest])
        check(rc == 0 and os.path.isfile(os.path.join(dest, "a (1).bin")), "同名自动改名")
        rc, out, _ = run(["get", "extract-me", "--list"])
        check(rc == 1, "文件码格式错误返回非零")
        check(landrop.extract_code(f"python3 landrop.py get {code}  ") == code, "extract_code 认整条命令")
        check(landrop.safe_local_name("CON.txt") == "_CON.txt"
              and landrop.safe_local_name("a:b?.txt. ") == "a_b_.txt", "Windows 文件名清洗")

        rc, _, _ = run(["send", os.path.join(src, "nope"), "--server", server, "--key", KEY])
        check(rc == 1, "不存在的文件报错")

        check(gid is not None, "输出授权 ID")
        rc, _, _ = run(["revoke", gid.group(1), "--server", server, "--key", KEY])
        check(rc == 0, "revoke 成功")
        rc, _, err = run(["get", code, "-o", dest])
        check(rc == 1, "撤销后取件失败")
        direct_checks(tmp)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\n{'失败 ' + str(len(FAILED)) + ' 项' if FAILED else '全部通过'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
