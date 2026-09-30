#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面窗口的端到端测试：驱动真实的 tkinter 控件走完 直连发送 / 接收 / 经服务器 / 取消。

没有显示环境（无 DISPLAY 且无 tkinter）时跳过并返回 0。Linux 上用 xvfb-run 运行：
    xvfb-run -a python tests/gui_check.py
"""
from __future__ import annotations

import faulthandler
import os
import socket
import sys
import tempfile
import time

faulthandler.dump_traceback_later(240, exit=True)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILED = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    try:
        import tkinter
        probe = tkinter.Tk()
        probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"跳过：没有可用的图形环境（{exc}）")
        return 0

    tmp = tempfile.mkdtemp(prefix="landrop-gui-")
    os.environ.update(LANDROP_DATA_DIR=os.path.join(tmp, "data"), LANDROP_PORT=str(free_port()),
                      LANDROP_SCOPE="local")
    os.environ.pop("LANDROP_URL", None)
    from landrop.gui import launcher

    app = launcher.LauncherApp()
    p = app.share
    errors = []
    p.messagebox.showerror = lambda t, m: errors.append((t, m))
    p.messagebox.showinfo = lambda t, m: errors.append((t, m))

    def pump(cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            app.root.update()
            if cond():
                return True
            time.sleep(0.02)
        return False

    src = os.path.join(tmp, "src")
    os.makedirs(os.path.join(src, "folder", "sub"))
    payload = os.urandom(250_000)
    for rel, data in (("big.bin", payload), ("folder/a.txt", b"A"), ("folder/sub/b.txt", b"B")):
        with open(os.path.join(src, rel), "wb") as f:
            f.write(data)

    print("[直连发送 → 接收]")
    check(app.notebook.tab(app.notebook.select(), "text") == "发送文件", "默认停在「发送文件」页")
    p._add(os.path.join(src, "big.bin"))
    p._add(os.path.join(src, "folder"))
    check(len(p.files) == 2, "添加文件与文件夹")
    check(p.mode.get() == "direct", "默认是直连模式")
    p.do_send()
    check(pump(lambda: bool(p.s_code.get())), "生成文件码")
    code = p.s_code.get()
    check(code.count("/") == 1 and "get " + code in p.s_cmd.get(), f"显示取件命令 {code}")
    host, _, token = code.partition("/")
    local_code = "127.0.0.1:" + host.rpartition(":")[2] + "/" + token     # 本机回环取件，避开网卡差异
    out = os.path.join(tmp, "recv")
    p.r_code.set("python -m landrop get " + local_code + "  ")
    p.r_dir.set(out)
    p.do_recv()
    check(pump(lambda: p.r_msg.get().startswith("完成")), "接收完成：" + p.r_msg.get()[:30])
    got = os.path.join(out, "big.bin")
    check(os.path.isfile(got) and open(got, "rb").read() == payload, "大文件内容一致")
    check(open(os.path.join(out, "folder", "sub", "b.txt")).read() == "B", "文件夹结构保留")
    check(pump(lambda: p.s_msg.get().startswith("已送达")), "发送端自动结束：" + p.s_msg.get()[:20])
    check(not p.busy and p.sender is None and p.s_code.get() == "", "结束后状态复位、文件码清除")

    print("\n[取消]")
    p.do_send()
    check(pump(lambda: bool(p.s_code.get())), "再次发送生成文件码")
    p.do_cancel()
    check(pump(lambda: p.s_msg.get() == "已取消"), "取消后停止监听")

    print("\n[经服务器]")
    app.start()
    check(app.running, "收集服务已启动")
    check(p.s_url.get().startswith("http://127.0.0.1:"), "服务地址自动填入")
    p.clear_files()
    p._add(os.path.join(src, "big.bin"))
    p.mode.set("server")
    p._mode_changed()
    p.s_max.set("1")
    p.do_send()
    check(pump(lambda: bool(p.s_code.get())), "经服务器生成文件码")
    code2 = p.s_code.get()
    code2 = "127.0.0.1:" + code2.partition("/")[0].rpartition(":")[2] + "/" + code2.partition("/")[2]
    out2 = os.path.join(tmp, "recv2")
    p.r_code.set(code2)
    p.r_dir.set(out2)
    p.do_recv()
    check(pump(lambda: p.r_msg.get().startswith("完成")), "经服务器取件完成")
    check(os.path.isfile(os.path.join(out2, "big.bin")), "文件已保存")
    del errors[:]
    p.do_recv()
    check(pump(lambda: p.r_msg.get() == "失败"), "一次性：第二次取件失败")
    check(bool(errors), "失败时弹出提示")

    p.clear_files()
    p._add(os.path.join(src, "folder"))
    p.do_send()
    check(any("暂不支持" in m[0] for m in errors), "经服务器发送文件夹给出明确提示")

    app.stop()
    app.root.destroy()
    print(f"\n{'失败 ' + str(len(FAILED)) + ' 项' if FAILED else '全部通过'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
