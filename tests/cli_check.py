#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""landrop.py 命令行端到端测试：真起服务 → send 多文件 → get 到指定目录 → 校验内容 → revoke。

用法: python3 tests/cli_check.py
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

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
