#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面窗口的端到端测试：驱动真实控件走完 直连发送 / 预览并勾选接收 / 经服务器（含文件夹）/
取消 / 主题切换 / 设置保存 / Windows 拖放。

没有图形环境（无 DISPLAY 或无 tkinter）时跳过并返回 0。Linux 上用 xvfb-run 运行：
    xvfb-run -a python tests/gui_check.py
"""
from __future__ import annotations

import faulthandler
import json
import os
import socket
import sys
import tempfile
import time

faulthandler.dump_traceback_later(300, exit=True)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILED = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg, flush=True)
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
        probe.update()          # 先处理掉 Tk 排队的空闲回调，否则销毁后会打印一段 Tcl 报错
        probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"跳过：没有可用的图形环境（{exc}）")
        return 0

    tmp = tempfile.mkdtemp(prefix="landrop-gui-")
    os.environ.update(LANDROP_DATA_DIR=os.path.join(tmp, "data"), LANDROP_PORT=str(free_port()),
                      LANDROP_SCOPE="local")
    os.environ.pop("LANDROP_URL", None)
    os.environ.pop("LANDROP_ADMIN_KEY", None)
    from landrop.gui import launcher, system

    app = launcher.LauncherApp()
    send, recv, svc = app.send, app.receive, app.service

    def pump(cond, timeout=30):
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
    for rel, data in (("big.bin", payload), ("folder/a.txt", b"A"), ("folder/sub/b.txt", b"B"),
                      ("other.txt", b"other")):
        with open(os.path.join(src, rel), "wb") as f:
            f.write(data)

    def local(code):          # 本机回环取件，避开网卡差异
        host, _, token = code.partition("/")
        return "127.0.0.1:" + host.rpartition(":")[2] + "/" + token

    print("[界面]")
    check(app.current == "send", "默认停在「发送」页")
    for key in app.PAGES:
        app.select(key)
        app.root.update()
    check(app.current == "settings", "四个页面都能切换")
    app.select("send")
    check(send.send_btn.state == "disabled", "没选文件时「生成文件码」不可点")

    print("\n[直连发送 → 预览 → 只接收勾选的文件]")
    send.add_paths([os.path.join(src, "big.bin"), os.path.join(src, "folder"), os.path.join(src, "other.txt")])
    check(len(send.files) == 3 and send.send_btn.state == "normal", "添加文件与文件夹")
    check(send.mode.get() == "direct", "默认直连")
    send.send()
    check(pump(lambda: bool(send.code.get())), "生成文件码")
    code = send.code.get()
    check(send.command.get().endswith("get " + code), "显示取件命令")
    check(send.web_url.get() == "http://" + code and send.qr.text == "http://" + code, "浏览器网址与二维码一致")
    check(send.busy and app.busy.get("send"), "发送中状态")
    out = os.path.join(tmp, "recv")
    recv.code.set("python -m landrop get " + local(code) + "  ")
    recv.dir.set(out)
    app.select("receive")
    recv.preview()
    check(pump(lambda: recv.listing is not None), "预览列出文件")
    check(len(recv.listing) == 4, f"预览文件数：{len(recv.listing or [])}")
    for f in recv.listing:
        recv.picks[f["id"]].set(f["name"] != "other.txt")
    recv.receive()
    check(pump(lambda: recv.task_pill.text == "完成"), "接收完成：" + recv.status.get()[:40])
    got = os.path.join(out, "big.bin")
    check(os.path.isfile(got) and open(got, "rb").read() == payload, "大文件内容一致")
    check(open(os.path.join(out, "folder", "sub", "b.txt")).read() == "B", "文件夹结构保留")
    check(not os.path.exists(os.path.join(out, "other.txt")), "未勾选的文件没有下载")
    check(send.busy and send.sender is not None and not send.sender.finished.is_set(),
          "部分取件后发送端继续等待，不消耗完整接收名额")
    check(len(recv.history) == 1, "部分取件记入最近接收")
    recv.preview()
    check(pump(lambda: recv.listing is not None), "再次预览剩余文件")
    for f in recv.listing:
        recv.picks[f["id"]].set(f["name"] == "other.txt")
    recv.receive()
    check(pump(lambda: recv.task_pill.text == "完成"), "补取剩余文件成功")
    check(os.path.isfile(os.path.join(out, "other.txt")), "剩余文件已保存")
    check(pump(lambda: send.state_pill.text == "已送达"), "发送端自动结束：" + send.status.get())
    check(not send.busy and send.sender is None, "结束后状态复位")
    check(len(recv.history) == 2, "两次接收都记入最近接收")

    print("\n[取消]")
    send.reset()
    send.send()
    check(pump(lambda: bool(send.code.get())), "再次发送生成文件码")
    send.cancel()
    check(pump(lambda: send.state_pill.text == "已取消"), "取消后停止监听")
    check(send.code.get() == "" and send.qr.text == "", "取消后清掉失效的文件码与二维码")

    print("\n[收集服务 + 经服务器发送（含文件夹）]")
    send.reset()
    app.select("service")
    svc.on.set(True)
    svc._toggled()
    check(svc.running and app.nav["service"].dot, "开关启动服务，导航显示运行中")
    check(send.url.get().startswith("http://127.0.0.1:") and send.key.get(), "服务地址与密钥自动填入")
    check(svc.url_var.get().startswith("http://127.0.0.1:"), "仅本机模式：访问地址是 127.0.0.1")
    check(not svc.qr.winfo_ismapped(), "仅本机模式不显示二维码")
    send.clear_files()
    send.add_paths([os.path.join(src, "big.bin"), os.path.join(src, "folder")])
    send.mode.set("server")
    send._mode_changed()
    send.max_dl.set("1")
    app.select("send")
    send.send()
    check(pump(lambda: bool(send.code.get())), "经服务器生成文件码：" + send.status.get()[:40])
    code2 = local(send.code.get())
    out2 = os.path.join(tmp, "recv2")
    recv.code.set(code2)
    recv.dir.set(out2)
    recv.receive()
    check(pump(lambda: recv.task_pill.text == "完成"), "经服务器取件完成")
    check(os.path.isfile(os.path.join(out2, "folder", "sub", "b.txt")), "经服务器发送的文件夹结构保留")
    recv.receive()
    check(pump(lambda: recv.task_pill.text == "失败"), "一次性：第二次取件失败")

    from landrop.gui import panels
    panels.messagebox.askokcancel = lambda *a, **k: True
    send.reset()
    send.send()
    check(pump(lambda: send.revoke_btn.winfo_ismapped()), "经服务器发送后可「撤销文件码」")
    code3 = local(send.code.get())
    send.revoke()
    check(pump(lambda: send.state_pill.text == "已取消" and not send.code.get()), "撤销后文件码清空")
    recv.code.set(code3)
    recv.dir.set(os.path.join(tmp, "recv3"))
    recv.receive()
    check(pump(lambda: recv.task_pill.text == "失败"), "撤销后的文件码取件失败")

    print("\n[主题与设置]")
    app.apply_theme("dark")
    app.root.update()
    check(app.root.cget("bg") == launcher.T.c["bg"] and launcher.T.mode == "dark", "切换深色主题")
    app.apply_theme("light")
    app.root.update()
    saved = json.load(open(system.settings_path(), encoding="utf-8"))
    check(saved.get("theme") == "light" and saved.get("send_mode") == "server", "设置已保存")

    if sys.platform.startswith("win"):
        print("\n[Windows 拖放]")
        from landrop.gui import windrop
        send.clear_files()
        send.mode.set("direct")
        svc.stop()
        windrop.simulate_drop(app.root.winfo_id(), [os.path.join(src, "other.txt"), os.path.join(src, "folder")])
        check(pump(lambda: len(send.files) == 2, 10), f"拖入两项后出现在发送列表：{send.files}")

    svc.stop()
    app.on_close()
    print(f"\n{'失败 ' + str(len(FAILED)) + ' 项' if FAILED else '全部通过'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
