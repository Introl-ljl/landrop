#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LAN Drop 桌面启动器（Windows / macOS 的图形界面，仅依赖标准库 tkinter）。

Linux 以命令行为主（landrop.py），本窗口在装有 tkinter 的 Linux 上也能用。


职责边界很清楚：它只负责"把服务跑起来并让用户看懂状态"——
选择数据目录与共享目录、设置端口、启动/停止服务、显示访问地址与管理员密钥、
提供"打开页面 / 打开目录 / 复制信息"的按钮。网页界面仍然是唯一的操作界面，
局域网内的参与者继续用浏览器访问，不需要安装任何东西。

命令行仍然完全可用：``python3 server.py --data-dir ...``。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import traceback
import webbrowser

from landrop.common import default_data_dir
from landrop.server import app as srv

from . import panels


def port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class LauncherApp:
    """启动器窗口。配置可用环境变量预填，便于脚本与运维：

    ``LANDROP_DATA_DIR`` / ``LANDROP_SHARE_DIR`` / ``LANDROP_PORT`` / ``LANDROP_SCOPE``
    """

    def __init__(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.messagebox = messagebox
        self.filedialog = filedialog

        self.root = tk.Tk()
        self.root.title("LAN Drop · 局域网文件收集与分享")
        self.root.minsize(680, 520)
        self.httpd = None
        self.thread = None
        self.running = False
        self.info = None

        pad = {"padx": 10, "pady": 6}
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        main = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(main, text="服务")
        main.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(main, text="共享目录（对方上传的文件存到这里）").grid(
            row=row, column=0, sticky="w", **pad)
        self.share_var = tk.StringVar(value=os.environ.get("LANDROP_SHARE_DIR", ""))
        ttk.Entry(main, textvariable=self.share_var).grid(row=row, column=1, sticky="ew", **pad)
        ttk.Button(main, text="选择…", command=self.pick_share).grid(row=row, column=2, **pad)
        row += 1
        ttk.Label(main, text="留空 = 数据目录的 files/ 下，按空间名分目录").grid(
            row=row, column=1, sticky="w", padx=10)
        row += 1

        ttk.Label(main, text="数据目录（链接、权限、校验记录）").grid(
            row=row, column=0, sticky="w", **pad)
        self.data_var = tk.StringVar(value=os.environ.get("LANDROP_DATA_DIR") or default_data_dir())
        ttk.Entry(main, textvariable=self.data_var).grid(row=row, column=1, sticky="ew", **pad)
        ttk.Button(main, text="选择…", command=self.pick_data).grid(row=row, column=2, **pad)
        row += 1

        ttk.Label(main, text="监听端口").grid(row=row, column=0, sticky="w", **pad)
        self.port_var = tk.StringVar(value=os.environ.get("LANDROP_PORT") or "8000")
        ttk.Entry(main, textvariable=self.port_var, width=10).grid(
            row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(main, text="可访问范围").grid(row=row, column=0, sticky="w", **pad)
        self.scope_var = tk.StringVar(value=os.environ.get("LANDROP_SCOPE") or "lan")
        scope = ttk.Frame(main)
        scope.grid(row=row, column=1, sticky="w", **pad)
        ttk.Radiobutton(scope, text="局域网（同一 WiFi/网段的设备都能访问）",
                        variable=self.scope_var, value="lan").pack(anchor="w")
        ttk.Radiobutton(scope, text="仅本机（只有这台电脑能访问）",
                        variable=self.scope_var, value="local").pack(anchor="w")
        row += 1

        self.start_btn = ttk.Button(main, text="启动服务", command=self.toggle)
        self.start_btn.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        row += 1

        self.status = tk.Text(main, height=15, wrap="word", state="disabled",
                              font=("Consolas", 10) if sys.platform.startswith("win")
                              else ("Menlo", 11))
        self.status.grid(row=row, column=0, columnspan=3, sticky="nsew", **pad)
        main.rowconfigure(row, weight=1)
        row += 1

        actions = ttk.Frame(main)
        actions.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        self.open_btn = ttk.Button(actions, text="打开页面", command=self.open_page, state="disabled")
        self.open_btn.pack(side="left")
        self.admin_btn = ttk.Button(actions, text="打开管理面板", command=self.open_admin,
                                    state="disabled")
        self.admin_btn.pack(side="left", padx=6)
        self.folder_btn = ttk.Button(actions, text="打开共享目录", command=self.open_folder,
                                     state="disabled")
        self.folder_btn.pack(side="left", padx=6)
        self.copy_btn = ttk.Button(actions, text="复制访问信息", command=self.copy_info,
                                   state="disabled")
        self.copy_btn.pack(side="left", padx=6)

        self.share = panels.SharePanels(
            self.root, self.notebook,
            default_url=f"http://127.0.0.1:{self.port_var.get().strip() or '8000'}")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.write("准备就绪。点击「启动服务」开始。\n")
        self.write("提示：首次启动会生成管理员密钥，用它在管理面板里创建分享链接。\n")
        self.write("要快速传文件：启动服务后切到「发送文件」页，选文件即可生成文件码。\n")

    # ---------------------------------------------------------------- 目录
    def pick_share(self):
        d = self.filedialog.askdirectory(title="选择共享目录")
        if d:
            self.share_var.set(d)

    def pick_data(self):
        d = self.filedialog.askdirectory(title="选择数据目录")
        if d:
            self.data_var.set(d)

    # ---------------------------------------------------------------- 输出
    def write(self, text: str):
        self.status.configure(state="normal")
        self.status.insert("end", text)
        self.status.see("end")
        self.status.configure(state="disabled")

    # ---------------------------------------------------------------- 服务
    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        data_dir = self.data_var.get().strip() or default_data_dir()
        share_dir = self.share_var.get().strip()
        try:
            port = int(self.port_var.get().strip() or "8000")
        except ValueError:
            self.messagebox.showerror("端口无效", "端口必须是数字。")
            return
        if not port_free(port):
            self.messagebox.showerror("端口被占用", f"端口 {port} 已被占用，换一个端口再试。")
            return
        host = "127.0.0.1" if self.scope_var.get() == "local" else "0.0.0.0"
        if share_dir:
            try:
                os.makedirs(share_dir, exist_ok=True)
            except OSError as exc:
                self.messagebox.showerror("目录不可写", f"{share_dir}\n{exc}")
                return
        try:
            self.info = srv.prepare(
                data_dir=data_dir, share_dir=share_dir, host=host, port=port,
            )
            self.httpd = srv.make_server(host, port)
        except Exception as exc:  # noqa: BLE001
            self.messagebox.showerror("启动失败", f"{exc}\n\n{traceback.format_exc(limit=3)}")
            return

        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.running = True
        self.start_btn.configure(text="停止服务")
        for b in (self.open_btn, self.admin_btn, self.folder_btn, self.copy_btn):
            b.configure(state="normal")

        info = self.info
        key = info["admin_key"]
        if not key:
            try:
                with open(info["admin_key_file"], encoding="utf-8") as f:
                    key = f.read().strip()
            except OSError:
                key = ""
        self.share.set_local_service(info["local_url"], key)
        self.write("\n" + "=" * 58 + "\n")
        self.write("  LAN Drop 已启动\n")
        self.write("=" * 58 + "\n")
        self.write(f"  访问地址 : {info['url']}\n")
        self.write(f"  本机访问 : {info['local_url']}\n")
        self.write(f"  管理面板 : {info['local_url']}/admin\n")
        self.write(f"  共享目录 : {info['share_dir']}\n")
        self.write(f"  数据目录 : {info['data_dir']}\n")
        if info["admin_key"]:
            self.write(f"  管理员密钥: {info['admin_key']}\n")
            self.write("     （已保存到数据目录的 admin-key.txt，可随时找回）\n")
        else:
            self.write(f"  管理员密钥: 见 {info['admin_key_file']}\n")
        self.write("=" * 58 + "\n")
        if info.get("import_report"):
            rep = info["import_report"]
            self.write(f"  导入：成功 {len(rep['imported'])}，失败 {len(rep['failed'])}\n")
        if host == "0.0.0.0":
            self.write("  把上面的「访问地址」发给同一局域网的人即可开始收集。\n")
            self.write("  首次启动若系统弹出防火墙提示，请选择「允许访问」。\n")
        else:
            self.write("  当前仅本机可访问；需要局域网共享请选择「局域网」。\n")

    def _serve(self):
        try:
            self.httpd.serve_forever()
        except Exception as exc:  # noqa: BLE001
            msg = f"\n服务异常：{exc!r}\n"      # 先取值：except 结束后 exc 会被删除，lambda 里再用会 NameError
            self.root.after(0, lambda: self.write(msg))

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        self.httpd = None
        self.running = False
        self.start_btn.configure(text="启动服务")
        for b in (self.open_btn, self.admin_btn, self.folder_btn, self.copy_btn):
            b.configure(state="disabled")
        self.write("\n服务已停止。已上传的文件与链接都保留在数据目录中，下次启动继续可用。\n")

    # ---------------------------------------------------------------- 动作
    def open_page(self):
        if self.info:
            webbrowser.open(self.info["local_url"])

    def open_admin(self):
        if self.info:
            webbrowser.open(self.info["local_url"] + "/admin")

    def open_folder(self):
        if not self.info:
            return
        path = self.info["share_dir"]
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:  # noqa: BLE001
            self.messagebox.showinfo("共享目录", f"{path}\n\n（无法自动打开：{exc}）")

    def copy_info(self):
        if not self.info:
            return
        text = (f"访问地址：{self.info['url']}\n"
                f"管理面板：{self.info['local_url']}/admin\n")
        if self.info["admin_key"]:
            text += f"管理员密钥：{self.info['admin_key']}\n"
        else:
            text += f"管理员密钥见：{self.info['admin_key_file']}\n"
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.write("\n已把访问信息复制到剪贴板。\n")

    def on_close(self):
        if self.running:
            if not self.messagebox.askokcancel("退出", "服务正在运行，退出会停止服务。\n"
                                                     "已上传的文件与链接都会保留。确定退出？"):
                return
            self.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main(argv=None):
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("未检测到 tkinter。请安装带 tkinter 的 Python，或改用命令行：\n"
              "  python3 server.py --data-dir ./data", file=sys.stderr)
        return 2
    import argparse
    ap = argparse.ArgumentParser(description="LAN Drop 桌面启动器")
    ap.add_argument("--autostart", action="store_true",
                    help="打开窗口后立即启动服务（适合开机自启）")
    ap.add_argument("--no-window", action="store_true",
                    help="不开窗口，只按环境变量启动服务（等价于 server.py）")
    args = ap.parse_args(argv)

    if args.no_window:
        return srv.main(["--data-dir", os.environ.get("LANDROP_DATA_DIR") or default_data_dir(),
                         "--host", "127.0.0.1" if os.environ.get("LANDROP_SCOPE") == "local"
                         else "0.0.0.0",
                         "--port", os.environ.get("LANDROP_PORT") or "8000"])

    app = LauncherApp()
    if args.autostart:
        app.root.after(300, app.start)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
