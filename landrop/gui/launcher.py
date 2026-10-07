#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Three-page desktop launcher. Local management never passes through the LAN HTTP API."""
from __future__ import annotations

import os
import sys
import threading
import traceback

from landrop import __version__
from landrop.common import lan_addresses, resolve_data_dir
from landrop.server.app import Runtime
from landrop.server.store import Store

from . import system
from . import widgets as W
from .theme import T, enable_dpi_awareness, style_titlebar


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
    """Window and local CLI use the same model and service owner."""

    PAGES = ("share", "receive", "settings")

    def __init__(self):
        import tkinter as tk
        from . import panels
        enable_dpi_awareness()
        self.store = Store(resolve_data_dir()[0])
        self.settings = self.store.settings()
        self.runtime = Runtime(self.store)
        self.cached_ip = "127.0.0.1"
        T.set_mode(self.settings.get("theme", "system"))
        self.root = tk.Tk(className="landrop")      # X11 WM_CLASS = ("landrop", "Landrop")，与 .desktop 的 StartupWMClass 对应
        W._IMG_CACHE.clear()
        T.init_tk(self.root)
        self.root.title("LAN Drop")
        self.root.configure(bg=T.c["bg"])
        self.root._roles = {"bg": "bg"}
        # 初始大小不超过屏幕（小屏 / 高缩放下 1040×720 可能放不下），并居中
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(T.px(1040), sw - T.px(40)), min(T.px(720), sh - T.px(90))
        self.root.minsize(min(T.px(880), w), min(T.px(600), h))
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self._logos = {}
        self._set_icon()
        self.busy: dict[str, bool] = {}
        self.bridge = panels.UiBridge(self.root)
        self.toast = W.Toast(self.root)

        side = W.frame(self.root, bg="sidebar", width=T.px(192))
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

        nav = W.frame(side, bg="sidebar")
        nav.pack(fill="x", padx=T.px(12))
        self.nav = {}
        for key, icon, text in (("share", "send", "分享"), ("receive", "download", "接收")):
            item = NavItem(nav, icon, text, lambda k=key: self.select(k))
            item.pack(fill="x", pady=T.px(2))
            self.nav[key] = item
        bottom = W.frame(side, bg="sidebar")
        bottom.pack(side="bottom", fill="x", padx=T.px(12), pady=(0, T.px(14)))
        self.nav["settings"] = NavItem(bottom, "settings", "设置", lambda: self.select("settings"))
        self.nav["settings"].pack(fill="x", pady=T.px(2))
        W.label(bottom, f"v{__version__}", role="muted", size=8, bg="sidebar").pack(
            anchor="w", padx=T.px(12), pady=(T.px(8), 0))

        self.share = panels.SharePage(self.content, self)
        self.receive = panels.ReceivePage(self.content, self)
        self.settings_page = panels.SettingsPage(self.content, self)
        self.pages = {"share": self.share, "receive": self.receive, "settings": self.settings_page}
        self.current = None

        for i, key in enumerate(self.PAGES, 1):
            self.root.bind_all(f"<Control-Key-{i}>", lambda e, k=key: self.select(k))
            if sys.platform == "darwin":
                self.root.bind_all(f"<Command-Key-{i}>", lambda e, k=key: self.select(k))
        self.root.bind_all("<Control-o>", lambda e: (self.select("share"), self.share.add_files()))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.select("share")
        style_titlebar(self.root, T.mode == "dark")
        self._enable_drop()
        def detect_address():
            ip = lan_addresses()[0]
            self.bridge.call(self._set_address, ip)
        threading.Thread(target=detect_address, daemon=True).start()

    def _set_address(self, ip):
        self.cached_ip = ip
        self.share.render_service()

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.toast.show("已复制")

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
        if "share" in getattr(self, "nav", {}):
            self.nav["share"].set(dot=on)

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
        self.settings = self.store.configure(theme=T.pref)

    # ------------------------------------------------------------ 拖放（Windows）
    def _enable_drop(self):
        if not sys.platform.startswith("win") or os.environ.get("LANDROP_NO_DND"):
            return
        try:
            from . import windrop
            windrop.enable(self.root, lambda paths: self.bridge.call(self._dropped, paths))
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _dropped(self, paths):
        self.select("share")
        if self.share.busy:
            self.toast.show("处理中，请稍候", "info")
            return
        self.share.add_paths(paths)
        self.toast.show(f"已添加 {len(paths)} 项")

    # ------------------------------------------------------------ 退出
    def on_close(self):
        from tkinter import messagebox
        jobs = [n for n, b in self.busy.items() if b]
        if jobs or self.runtime.active_transfers:
            if not messagebox.askokcancel("退出 LAN Drop", "仍有任务正在进行，退出会中断服务。完整文件保留。", parent=self.root):
                return
        self.save_settings()
        self.receive.shutdown()
        self.runtime.stop()
        self.runtime.close()
        self.share.shutdown()
        self.toast.hide()
        self.bridge.close()
        self.store.close()
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
    app.runtime.start(0, "127.0.0.1")
    if not app.runtime.running:
        return 1
    with urllib.request.urlopen(app.runtime.info["local_url"] + "/", timeout=10) as r:
        ok = r.status == 200 and b"LAN Drop" in r.read()
    app.root.update()
    app.runtime.stop()
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
    ap.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args([arg for arg in (sys.argv[1:] if argv is None else argv) if not arg.startswith("-psn_")])

    app = LauncherApp()
    if args.smoke_test:
        try:
            return smoke_test(app)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return 1
        finally:
            app.runtime.close()
            app.share.shutdown()
            app.toast.hide()
            app.bridge.close()
            app.store.close()
            app.root.destroy()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
