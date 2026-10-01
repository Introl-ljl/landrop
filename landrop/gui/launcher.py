#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LAN Drop 桌面窗口（Windows / macOS / Linux，仅依赖标准库 tkinter）。

布局参照 LocalSend：左侧导航（发送 / 接收 / 收集服务 / 设置），右侧卡片式内容；
配色与网页共用一套设计令牌（theme.py），支持浅色 / 深色并跟随系统。

窗口与命令行是同一个程序的两个入口：安装包里 ``LAN Drop``（窗口）与 ``landrop``（命令行）
共用一份运行时，``landrop gui`` 也能打开本窗口。
"""
from __future__ import annotations

import os
import sys
import traceback

from landrop import __version__

from . import system
from . import widgets as W
from .theme import T, enable_dpi_awareness, style_titlebar


class LogTee:
    """把服务端日志（print 到 stdout）同时送到原输出和窗口里的「服务日志」。"""

    def __init__(self, original, sink):
        self.original, self.sink = original, sink
        self.encoding = "utf-8"

    def write(self, text):
        if self.original is not None:
            try:
                self.original.write(text)
            except Exception:  # noqa: BLE001
                pass
        if text:
            self.sink(text)
        return len(text)

    def flush(self):
        if self.original is not None:
            try:
                self.original.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self):
        return False


class NavItem(W.Canvas):
    """侧边栏导航项：选中时是浮起的白色圆角块，可带状态小圆点。"""

    def __init__(self, parent, icon, text, command):
        self.icon, self.text, self.command = icon, text, command
        self.active = self._hover = False
        self.dot = False
        self._h = T.px(40)
        super().__init__(parent, height=self._h, cursor="hand2")
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self._sethover(True))
        self.bind("<Leave>", lambda e: self._sethover(False))
        self.bind("<Configure>", lambda e: self.draw())

    def _sethover(self, on):
        self._hover = on
        self.draw()

    def set(self, active=None, dot=None):
        if active is not None:
            self.active = active
        if dot is not None:
            self.dot = dot
        self.draw()

    def draw(self):
        self.delete("all")
        c = T.c
        bg = W.bg_of(self.master)
        self.configure(bg=bg)
        w, h = self.winfo_width(), self._h
        if w <= 1:
            return
        fill = bg
        if self.active:
            fill = c["nav_active"]
            W.rounded(self, 0, 0, w, h, T.px(10), fill, c["line"] if T.mode == "light" else None, 1, bg)
        elif self._hover:
            fill = c["hover"]
            W.rounded(self, 0, 0, w, h, T.px(10), fill, None, 1, bg)
        fg = c["text"] if self.active else c["text2"]
        ic = c["accent_text"] if self.active else c["muted"]
        isz = T.px(18)
        self.create_image(T.px(12), h // 2, image=W.icon_image(self.icon, isz, ic, fill), anchor="w")
        self.create_text(T.px(12) + isz + T.px(12), h // 2, text=self.text, fill=fg,
                         font=T.font(10, "bold" if self.active else "normal"), anchor="w")
        if self.dot:
            d = T.px(8)
            x = w - T.px(16)
            W.rounded(self, x, (h - d) // 2, x + d, (h - d) // 2 + d, d // 2, c["accent"], None, 1, fill)

    def recolor(self):
        self.draw()


class LauncherApp:
    """主窗口。配置可用环境变量预填（脚本 / 测试用）：
    ``LANDROP_DATA_DIR`` / ``LANDROP_SHARE_DIR`` / ``LANDROP_PORT`` / ``LANDROP_SCOPE`` / ``LANDROP_URL``
    """

    PAGES = ("send", "receive", "service", "settings")

    def __init__(self):
        import tkinter as tk
        from . import panels
        enable_dpi_awareness()
        self.settings = system.load_settings()
        T.set_mode(self.settings.get("theme", "system"))
        self.root = tk.Tk(className="LANDrop")
        T.init_tk(self.root)
        self.root.title("LAN Drop")
        self.root.configure(bg=T.c["bg"])
        self.root._roles = {"bg": "bg"}
        self.root.minsize(T.px(880), T.px(600))
        self.root.geometry(f"{T.px(1040)}x{T.px(720)}")
        self._logos = {}
        self._set_icon()
        self.busy: dict[str, bool] = {}
        self.bridge = panels.UiBridge(self.root)
        self.toast = W.Toast(self.root)
        self.service = None

        side = W.frame(self.root, bg="sidebar", width=T.px(224))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        W.frame(self.root, bg="line", width=max(1, T.px(1))).pack(side="left", fill="y")
        self.content = W.frame(self.root)
        self.content.pack(side="left", fill="both", expand=True)

        brand = W.frame(side, bg="sidebar")
        brand.pack(fill="x", padx=T.px(18), pady=(T.px(22), T.px(22)))
        logo = tk.Label(brand, image=self.logo(32), bd=0, bg=T.c["sidebar"])
        logo._roles = {"bg": "sidebar"}
        logo.pack(side="left")
        bt = W.frame(brand, bg="sidebar")
        bt.pack(side="left", padx=(T.px(10), 0))
        W.label(bt, "LAN Drop", size=12, weight="bold", bg="sidebar").pack(anchor="w")
        W.label(bt, "局域网文件互传", role="muted", size=9, bg="sidebar").pack(anchor="w")

        nav = W.frame(side, bg="sidebar")
        nav.pack(fill="x", padx=T.px(12))
        self.nav = {}
        for key, icon, text in (("send", "send", "发送"), ("receive", "download", "接收"),
                                ("service", "server", "收集服务")):
            item = NavItem(nav, icon, text, lambda k=key: self.select(k))
            item.pack(fill="x", pady=T.px(2))
            self.nav[key] = item
        bottom = W.frame(side, bg="sidebar")
        bottom.pack(side="bottom", fill="x", padx=T.px(12), pady=(0, T.px(14)))
        self.nav["settings"] = NavItem(bottom, "settings", "设置", lambda: self.select("settings"))
        self.nav["settings"].pack(fill="x", pady=T.px(2))
        W.label(bottom, f"v{__version__}", role="muted", size=8, bg="sidebar").pack(
            anchor="w", padx=T.px(12), pady=(T.px(8), 0))

        self.send = panels.SendPage(self.content, self)
        self.receive = panels.ReceivePage(self.content, self)
        self.service = panels.ServicePage(self.content, self)
        self.settings_page = panels.SettingsPage(self.content, self)
        self.pages = {"send": self.send, "receive": self.receive, "service": self.service,
                      "settings": self.settings_page}
        self.current = None

        self._stdout = sys.stdout
        sys.stdout = LogTee(self._stdout, lambda t: self.bridge.call(self.service.append_log, t))

        for i, key in enumerate(self.PAGES, 1):
            self.root.bind_all(f"<Control-Key-{i}>", lambda e, k=key: self.select(k))
            if sys.platform == "darwin":
                self.root.bind_all(f"<Command-Key-{i}>", lambda e, k=key: self.select(k))
        self.root.bind_all("<Control-o>", lambda e: (self.select("send"), self.send.add_files()))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.select("send")
        style_titlebar(self.root, T.mode == "dark")
        self._enable_drop()

    # ------------------------------------------------------------ 外观
    def _set_icon(self):
        import tkinter as tk
        from .icon import PNG
        try:
            imgs = [tk.PhotoImage(data="".join(PNG[s])) for s in (16, 32, 48, 64, 128)]
            self._icon_imgs = imgs
            self.root.iconphoto(True, *imgs)
        except tk.TclError:
            pass

    def logo(self, size):
        import tkinter as tk
        from .icon import PNG
        if size not in self._logos:
            best = min(PNG, key=lambda s: (s < T.px(size), abs(s - T.px(size))))
            img = tk.PhotoImage(data="".join(PNG[best]))
            factor = best // T.px(size)
            if factor > 1:
                img = img.subsample(factor)
            self._logos[size] = img
        return self._logos[size]

    def apply_theme(self, pref):
        T.set_mode(pref)
        self.root.configure(bg=T.c["bg"])
        W.recolor_tree(self.root)
        for item in self.nav.values():
            item.draw()
        style_titlebar(self.root, T.mode == "dark")
        self.save_settings()

    # ------------------------------------------------------------ 导航与状态
    def select(self, key):
        if self.current == key:
            return
        if self.current:
            self.pages[self.current].pack_forget()
        self.current = key
        self.pages[key].pack(fill="both", expand=True)
        for k, item in self.nav.items():
            item.set(active=(k == key))
        self.pages[key].on_show()

    def set_busy(self, name, busy):
        self.busy[name] = busy

    def set_service_state(self, on):
        if "service" in getattr(self, "nav", {}):
            self.nav["service"].set(dot=on)

    def notify(self, title, message):
        """后台完成时提醒：窗口不在前台就响一声并在任务栏闪烁（Windows）。"""
        self.toast.show(f"{title}：{message}" if len(message) < 40 else title, "ok", 3500)
        try:
            if self.root.focus_displayof() is None:
                self.root.bell()
                if sys.platform.startswith("win"):
                    import ctypes
                    hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                    ctypes.windll.user32.FlashWindow(hwnd, True)
        except Exception:  # noqa: BLE001
            pass

    def save_settings(self):
        s = dict(self.settings)
        s.update(theme=T.pref, send_mode=self.send.mode.get(), timeout=self.send.timeout.get(),
                 receivers=self.send.receivers.get(), expire=self.send.expire.get(),
                 max_downloads=self.send.max_dl.get(), recv_dir=self.receive.dir.get(),
                 overwrite=bool(self.receive.overwrite.get()))
        url = self.send.url.get().strip()
        if url and not (self.service.running and self.service.info and url == self.service.info["local_url"]):
            s["server_url"] = url
        if not os.environ.get("LANDROP_PORT"):
            s.update(port=self.service.port.get(), scope=self.service.scope.get(),
                     share_dir=self.service.share_dir.get(), service_address=self.service.address.get())
        if not os.environ.get("LANDROP_DATA_DIR"):
            s["data_dir"] = self.service.data_dir.get()
        self.settings = s
        system.save_settings(s)

    # ------------------------------------------------------------ 拖放（Windows）
    def _enable_drop(self):
        if not sys.platform.startswith("win") or os.environ.get("LANDROP_NO_DND"):
            return
        try:
            from . import windrop
            windrop.enable(self.root, self._dropped)
            self.send.drop_hint.configure(text="把文件或文件夹拖进窗口，或点下面的按钮选择")
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _dropped(self, paths):
        self.select("send")
        if self.send.busy:
            self.toast.show("正在发送，完成后再添加", "info")
            return
        self.send.reset()
        self.send.add_paths(paths)
        self.toast.show(f"已添加 {len(paths)} 项")

    # ------------------------------------------------------------ 兼容：自检与旧调用
    @property
    def running(self):
        return self.service.running

    @property
    def info(self):
        return self.service.info

    def start(self):
        self.service.start()

    def stop(self):
        self.service.stop()

    # ------------------------------------------------------------ 退出
    def on_close(self):
        from tkinter import messagebox
        jobs = [n for n, b in self.busy.items() if b]
        if self.service.running:
            jobs.append("service")
        if jobs:
            what = {"send": "正在发送", "receive": "正在接收", "service": "收集服务正在运行"}
            text = "、".join(what[j] for j in jobs)
            if not messagebox.askokcancel("退出 LAN Drop", f"{text}，退出会中断。\n"
                                          "已上传的文件与链接都会保留。确定退出？", parent=self.root):
                return
        self.save_settings()
        self.send.shutdown()
        self.receive.shutdown()
        if self.service.running:
            self.service.stop()
        sys.stdout = self._stdout
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def _ensure_streams():
    """窗口版（PyInstaller --windowed）没有控制台，sys.stdout/stderr 是 None，
    服务端日志里的 print 会直接抛异常；换成空设备。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def smoke_test(app) -> int:
    """打包产物自检：每个页面都能显示、两种主题都能切换、服务能启动并响应、能正常停止。"""
    import urllib.request
    for key in app.PAGES:
        app.select(key)
        app.root.update()
    app.apply_theme("dark")
    app.root.update()
    app.apply_theme("light")
    app.root.update()
    app.start()
    if not app.running:
        return 1
    with urllib.request.urlopen(app.info["local_url"] + "/", timeout=10) as r:
        ok = r.status == 200 and b"LAN Drop" in r.read()
    app.root.update()
    app.stop()
    app.root.update()
    return 0 if ok else 1


def main(argv=None):
    _ensure_streams()
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("未检测到 tkinter。请安装带 tkinter 的 Python，或改用命令行：landrop --help",
              file=sys.stderr)
        return 2
    import argparse
    ap = argparse.ArgumentParser(prog="landrop gui", description="LAN Drop 桌面窗口")
    ap.add_argument("--autostart", action="store_true",
                    help="打开窗口后立即启动收集服务（适合开机自启）")
    ap.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args, _unknown = ap.parse_known_args(argv)     # macOS 从 Finder 启动可能带 -psn_… 参数

    app = LauncherApp()
    if args.smoke_test:
        try:
            return smoke_test(app)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return 1
        finally:
            sys.stdout = app._stdout
            app.root.destroy()
    if args.autostart:
        app.root.after(300, lambda: (app.select("service"), app.start()))
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
