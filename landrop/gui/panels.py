# -*- coding: utf-8 -*-
"""桌面窗口里的「发送 / 接收」页（Windows / macOS 的图形界面；Linux 用命令行）。

发送有两种方式：
  * 直连（默认）：本机临时监听，把文件码发给对方，对方 ``landrop get`` 或在「接收」页取件。
    不需要服务、不需要管理员密钥；发送期间窗口要保持打开。
  * 经服务器：上传到常驻的收集服务，生成一次性取件码，对方随时取，不要求本机在线。

真正的传输都调用 ``landrop.direct`` / ``landrop.remote``，与命令行行为一致。
耗时操作都在后台线程里，通过队列回到界面线程更新控件。
"""
from __future__ import annotations

import os
import queue
import sys
import threading

from landrop import common, direct, remote


def open_path(path: str):
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        import subprocess
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


class UiBridge:
    """后台线程 → 界面线程：线程里 ``call(fn, *args)``，界面线程轮询执行。"""

    def __init__(self, root):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self._poll()

    def call(self, fn, *args):
        self.q.put((fn, args))

    def _poll(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)


class SharePanels:
    def __init__(self, root, notebook, default_url="http://127.0.0.1:8000"):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk, self.ttk = tk, ttk
        self.filedialog, self.messagebox = filedialog, messagebox
        self.root = root
        self.ui = UiBridge(root)
        self.files: list[str] = []
        self.sender: direct.DirectSender | None = None
        self.busy = False            # 发送中（直连等待 / 上传中）
        self.cancel_ev = threading.Event()   # 经服务器上传的取消信号
        self.last_grant = ""
        self.font = ("Consolas", 10) if sys.platform.startswith("win") else ("Menlo", 11)

        self.send_tab = ttk.Frame(notebook, padding=14)
        self.recv_tab = ttk.Frame(notebook, padding=14)
        notebook.add(self.send_tab, text="发送文件")
        notebook.add(self.recv_tab, text="接收文件")
        self._build_send(default_url)
        self._build_recv()

    def shutdown(self):
        """窗口关闭时：结束仍在等待的直连发送。"""
        if self.sender is not None:
            self.sender.stop("cancelled")

    # ================================================================ 发送页
    def _build_send(self, default_url):
        tk, ttk = self.tk, self.ttk
        f = self.send_tab
        f.columnconfigure(1, weight=1)
        pad = {"padx": 8, "pady": 4}

        ttk.Label(f, text="要发送的文件").grid(row=0, column=0, sticky="nw", **pad)
        box = ttk.Frame(f)
        box.grid(row=0, column=1, sticky="nsew", **pad)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(box, height=5, selectmode="extended")
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box, command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)
        f.rowconfigure(0, weight=1)
        btns = ttk.Frame(f)
        btns.grid(row=0, column=2, sticky="n", **pad)
        ttk.Button(btns, text="添加文件…", command=self.add_files).pack(fill="x")
        ttk.Button(btns, text="添加文件夹…", command=self.add_folder).pack(fill="x", pady=4)
        ttk.Button(btns, text="移除选中", command=self.remove_files).pack(fill="x")
        ttk.Button(btns, text="清空", command=self.clear_files).pack(fill="x", pady=4)

        ttk.Label(f, text="发送方式").grid(row=1, column=0, sticky="nw", **pad)
        mode = ttk.Frame(f)
        mode.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        self.mode = tk.StringVar(value="direct")
        ttk.Radiobutton(mode, text="直连：对方从我这台电脑取（无需服务；发送期间请保持窗口打开）",
                        variable=self.mode, value="direct", command=self._mode_changed).pack(anchor="w")
        ttk.Radiobutton(mode, text="经服务器：上传到收集服务，对方随时可取（需要服务地址与管理员密钥）",
                        variable=self.mode, value="server", command=self._mode_changed).pack(anchor="w")

        self.opts = ttk.Frame(f)
        self.opts.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
        self.opts.columnconfigure(1, weight=1)
        # 直连选项
        self.d_timeout = tk.StringVar(value="30")
        self.d_frame = ttk.Frame(self.opts)
        ttk.Label(self.d_frame, text="无人取件时自动退出（分钟）").pack(side="left")
        ttk.Spinbox(self.d_frame, from_=1, to=1440, width=6, textvariable=self.d_timeout).pack(
            side="left", padx=6)
        # 服务器选项
        self.s_url = tk.StringVar(value=os.environ.get("LANDROP_URL") or default_url)
        try:
            key = os.environ.get("LANDROP_ADMIN_KEY") or remote.read_admin_key()
        except common.CliError:
            key = ""
        self.s_key = tk.StringVar(value=key)
        self.s_expire = tk.StringVar(value="24")
        self.s_max = tk.StringVar(value="1")
        self.s_frame = ttk.Frame(self.opts)
        self.s_frame.columnconfigure(1, weight=1)
        ttk.Label(self.s_frame, text="服务地址").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(self.s_frame, textvariable=self.s_url).grid(row=0, column=1, columnspan=3,
                                                              sticky="ew", padx=6)
        ttk.Label(self.s_frame, text="管理员密钥").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(self.s_frame, textvariable=self.s_key, show="•").grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=6)
        ttk.Label(self.s_frame, text="有效期（小时，0=永久）").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(self.s_frame, from_=0, to=8760, width=6, textvariable=self.s_expire).grid(
            row=2, column=1, sticky="w", padx=6)
        ttk.Label(self.s_frame, text="可取次数（0=不限）").grid(row=2, column=2, sticky="e")
        ttk.Spinbox(self.s_frame, from_=0, to=1000, width=6, textvariable=self.s_max).grid(
            row=2, column=3, sticky="w", padx=6)
        self._mode_changed()

        actions = ttk.Frame(f)
        actions.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        actions.columnconfigure(0, weight=1)
        self.send_btn = ttk.Button(actions, text="生成文件码并开始发送", command=self.do_send)
        self.send_btn.grid(row=0, column=0, sticky="ew")
        self.cancel_btn = ttk.Button(actions, text="取消", command=self.do_cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1, padx=(6, 0))
        self.s_bar = ttk.Progressbar(f, maximum=100)
        self.s_bar.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        self.s_msg = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.s_msg).grid(row=5, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(f, text="文件码").grid(row=6, column=0, sticky="w", **pad)
        self.s_code = tk.StringVar()
        ttk.Entry(f, textvariable=self.s_code, state="readonly", font=self.font).grid(
            row=6, column=1, sticky="ew", **pad)
        self.copy_code_btn = ttk.Button(f, text="复制文件码", command=lambda: self._copy(self.s_code),
                                        state="disabled")
        self.copy_code_btn.grid(row=6, column=2, sticky="ew", **pad)
        ttk.Label(f, text="取件命令").grid(row=7, column=0, sticky="w", **pad)
        self.s_cmd = tk.StringVar()
        ttk.Entry(f, textvariable=self.s_cmd, state="readonly", font=self.font).grid(
            row=7, column=1, sticky="ew", **pad)
        self.copy_cmd_btn = ttk.Button(f, text="复制命令", command=lambda: self._copy(self.s_cmd),
                                       state="disabled")
        self.copy_cmd_btn.grid(row=7, column=2, sticky="ew", **pad)
        self.s_log = tk.Text(f, height=6, wrap="word", state="disabled", font=self.font)
        self.s_log.grid(row=8, column=0, columnspan=3, sticky="nsew", **pad)
        f.rowconfigure(8, weight=1)

    def set_local_service(self, url: str, key: str):
        """本窗口的「收集服务」启动后，自动填好服务器模式的地址与密钥。"""
        self.s_url.set(url)
        if key:
            self.s_key.set(key)

    def _mode_changed(self):
        self.d_frame.pack_forget()
        self.s_frame.pack_forget()
        (self.d_frame if self.mode.get() == "direct" else self.s_frame).pack(fill="x")

    # ---- 文件列表
    def _add(self, path: str):
        path = os.path.abspath(path)
        if path in self.files:
            return
        self.files.append(path)
        if os.path.isdir(path):
            label = f"📁 {os.path.basename(path)}/   （文件夹）"
        else:
            label = f"{os.path.basename(path)}   ({common.human(os.path.getsize(path))})"
        self.listbox.insert("end", label)

    def add_files(self):
        for p in self.filedialog.askopenfilenames(title="选择要发送的文件（可多选）"):
            self._add(p)

    def add_folder(self):
        d = self.filedialog.askdirectory(title="选择要发送的文件夹")
        if d:
            self._add(d)

    def remove_files(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.files[i]

    def clear_files(self):
        self.listbox.delete(0, "end")
        self.files.clear()

    # ---- 界面状态
    def _log_send(self, text):
        self.s_log.configure(state="normal")
        self.s_log.insert("end", text + "\n")
        self.s_log.see("end")
        self.s_log.configure(state="disabled")

    def _set_busy(self, busy: bool):
        self.busy = busy
        self.send_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    def _show_code(self, code: str, command: str):
        self.s_code.set(code)
        self.s_cmd.set(command)
        for b in (self.copy_code_btn, self.copy_cmd_btn):
            b.configure(state="normal")

    def _copy(self, var):
        self.root.clipboard_clear()
        self.root.clipboard_append(var.get())
        self.s_msg.set("已复制到剪贴板")

    def _fail(self, title, msg):
        self._set_busy(False)
        self.s_msg.set("失败")
        self._log_send(f"✗ {msg}")
        self.messagebox.showerror(title, msg)

    # ---- 发送入口
    def do_send(self):
        if self.busy:
            return
        if not self.files:
            self.messagebox.showinfo("没有文件", "请先点「添加文件…」或「添加文件夹…」。")
            return
        self.s_code.set("")
        self.s_cmd.set("")
        self.s_bar["value"] = 0
        self.cancel_ev.clear()
        self.s_log.configure(state="normal")
        self.s_log.delete("1.0", "end")
        self.s_log.configure(state="disabled")
        if self.mode.get() == "direct":
            self._send_direct()
        else:
            self._send_via_server()

    def do_cancel(self):
        self.cancel_ev.set()
        if self.sender is not None:
            self.sender.stop("cancelled")

    # ---- 直连
    def _send_direct(self):
        try:
            timeout = float(self.d_timeout.get() or 30)
            assert timeout > 0
        except (ValueError, AssertionError):
            self.messagebox.showerror("时长无效", "等待时长必须是大于 0 的数字（分钟）。")
            return
        paths = list(self.files)
        self._set_busy(True)
        self.s_msg.set("正在计算校验值…")
        ui = self.ui

        def work():
            try:
                sender = direct.DirectSender(
                    paths, receivers=1,
                    on_event=lambda m: ui.call(self._log_send, "· " + m))
                for w in sender.warnings:
                    ui.call(self._log_send, "· 注意：" + w)
                sender.start()          # 先监听：文件码立即可用，校验值后台补算
                ips = direct.lan_addresses()
                code = common.make_code(f"http://{ips[0]}:{sender.port}", sender.token)
                ui.call(self._direct_ready, sender, code, ips,
                        len(sender.entries), sender.total_bytes)
                names = [e["name"] for e in sender.entries]
                done = [0]

                def hashed(name):
                    done[0] += 1
                    ui.call(self._hash_progress, done[0], len(names), name)

                sender.start_hashing(hashed)
                reason = sender.wait(timeout * 60)
                ui.call(self._direct_finished, reason)
            except common.CliError as exc:
                ui.call(self._fail, "发送失败", str(exc))
            except Exception as exc:  # noqa: BLE001
                ui.call(self._fail, "发送失败", f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def _hash_progress(self, i, n, name):
        self.s_bar["value"] = i * 100 // max(n, 1)
        self.s_msg.set(f"正在计算校验值 {i}/{n}：{name}")

    def _direct_ready(self, sender, code, ips, n, total):
        self.sender = sender
        self._show_code(code, f"{common.cmd_prefix()} get {code}")
        self.s_msg.set(f"等待接收端连接…（{n} 个文件，{common.human(total)}；"
                       "校验值后台计算中，对方会自动等待）")
        self._log_send("提示：系统防火墙首次弹窗请选择「允许访问」；传输为明文 HTTP，仅限可信局域网。")
        if len(ips) > 1:
            self._log_send("其他可用地址（对方连不上时换一个 IP）：" +
                           "  ".join(f"{ip}:{sender.port}" for ip in ips[1:]))

    def _direct_finished(self, reason):
        self.sender = None
        self._set_busy(False)
        text = {"done": "已送达，发送结束", "timeout": "超时无人取件，已停止",
                "failures": "错误尝试过多，已停止", "cancelled": "已取消"}.get(reason, reason)
        self.s_msg.set(text)
        self._log_send(text)
        for b in (self.copy_code_btn, self.copy_cmd_btn):
            b.configure(state="disabled")
        self.s_code.set("")
        self.s_cmd.set("")

    # ---- 经服务器
    def _send_via_server(self):
        try:
            expire = float(self.s_expire.get() or 24)
            max_dl = int(self.s_max.get() or 0)
            assert expire >= 0 and max_dl >= 0
        except (ValueError, AssertionError):
            self.messagebox.showerror("参数无效", "有效期与可取次数必须是不小于 0 的数字。")
            return
        url, key = self.s_url.get().strip(), self.s_key.get().strip()
        if not key:
            self.messagebox.showerror("缺少密钥", "请填写管理员密钥（本窗口启动服务后会自动填入）。")
            return
        if any(os.path.isdir(p) for p in self.files):
            self.messagebox.showinfo("暂不支持", "经服务器发送暂不支持文件夹，请改用直连或先打包。")
            return
        files = list(self.files)
        self._set_busy(True)
        self.s_msg.set("连接中…")
        ui = self.ui

        def progress(name, done, total):
            ui.call(self._upload_progress, name, done, total)

        def work():
            try:
                res = remote.send_files(url, key, files, expire, "", "", progress, max_dl,
                                        self.cancel_ev.is_set)
                ui.call(self._server_done, res)
            except common.CliError as exc:
                if self.cancel_ev.is_set() and "已取消" in str(exc):
                    ui.call(self._server_cancelled)
                else:
                    ui.call(self._fail, "发送失败", str(exc))
            except Exception as exc:  # noqa: BLE001
                ui.call(self._fail, "发送失败", f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def _server_cancelled(self):
        self._set_busy(False)
        self.s_msg.set("已取消")
        self._log_send("已取消上传；服务端会自动清理未完成的传输")

    def _upload_progress(self, name, done, total):
        self.s_bar["value"] = 100 if not total else done * 100 // total
        self.s_msg.set(f"正在上传 {name}  {common.human(done)}/{common.human(total)}")

    def _server_done(self, res):
        self._set_busy(False)
        self.last_grant = res["grant_id"]
        self.s_bar["value"] = 100
        self._show_code(res["code"], res["command"])
        times = f"可取 {res['max_downloads']} 次" if res["max_downloads"] else "取件次数不限"
        self.s_msg.set(f"完成：{res['count']} 个文件，{common.human(res['bytes'])}，{times}。"
                       "把文件码或取件命令发给对方即可，到期/取完后服务端自动清理。")

    # ================================================================ 接收页
    def _build_recv(self):
        tk, ttk = self.tk, self.ttk
        f = self.recv_tab
        f.columnconfigure(1, weight=1)
        pad = {"padx": 8, "pady": 4}

        ttk.Label(f, text="文件码或取件命令").grid(row=0, column=0, sticky="w", **pad)
        self.r_code = tk.StringVar()
        ttk.Entry(f, textvariable=self.r_code).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(f, text="粘贴", command=self.paste_code).grid(row=0, column=2, **pad)

        ttk.Label(f, text="保存到").grid(row=1, column=0, sticky="w", **pad)
        self.r_dir = tk.StringVar(value=self._default_download_dir())
        ttk.Entry(f, textvariable=self.r_dir).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(f, text="选择…", command=self.pick_dir).grid(row=1, column=2, **pad)

        self.r_force = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="同名文件直接覆盖（默认自动改名）", variable=self.r_force).grid(
            row=2, column=1, sticky="w", **pad)

        self.recv_btn = ttk.Button(f, text="下载", command=self.do_recv)
        self.recv_btn.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        self.r_bar = ttk.Progressbar(f, maximum=100)
        self.r_bar.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        self.r_msg = tk.StringVar()
        ttk.Label(f, textvariable=self.r_msg).grid(row=5, column=0, columnspan=3, sticky="w", **pad)
        self.r_log = tk.Text(f, height=10, wrap="word", state="disabled", font=self.font)
        self.r_log.grid(row=6, column=0, columnspan=3, sticky="nsew", **pad)
        f.rowconfigure(6, weight=1)
        ttk.Button(f, text="打开保存目录", command=self.open_dir).grid(
            row=7, column=1, sticky="w", **pad)

    @staticmethod
    def _default_download_dir():
        d = os.path.join(os.path.expanduser("~"), "Downloads")
        return d if os.path.isdir(d) else os.path.expanduser("~")

    def paste_code(self):
        try:
            self.r_code.set(common.extract_code(self.root.clipboard_get()))
        except self.tk.TclError:
            pass

    def pick_dir(self):
        d = self.filedialog.askdirectory(title="选择保存目录", initialdir=self.r_dir.get() or None)
        if d:
            self.r_dir.set(d)

    def open_dir(self):
        d = os.path.expanduser(self.r_dir.get().strip())
        if os.path.isdir(d):
            open_path(d)

    def _log_recv(self, text):
        self.r_log.configure(state="normal")
        self.r_log.insert("end", text)
        self.r_log.see("end")
        self.r_log.configure(state="disabled")

    def do_recv(self):
        code = common.extract_code(self.r_code.get())
        directory = os.path.expanduser(self.r_dir.get().strip())
        if not code or not directory:
            self.messagebox.showinfo("信息不全", "请填写文件码和保存目录。")
            return
        force = self.r_force.get()
        self.r_code.set(code)
        self.recv_btn.configure(state="disabled")
        self.r_bar["value"] = 0
        self.r_msg.set("连接中…")
        ui = self.ui

        def prog(name, done, total):
            ui.call(self._recv_progress, name, done, total)

        def one(item, target):
            mark = "已校验 SHA-256" if item.get("verified") else "对端未提供校验值"
            ui.call(self._log_recv, f"✓ {target}  ({common.human(item['size'])}，{mark})\n")

        def work():
            try:
                saved = remote.fetch_files(code, directory, None, force, prog, one)
                ui.call(self._recv_done, len(saved), directory)
            except common.CliError as exc:
                ui.call(self._recv_fail, str(exc))
            except Exception as exc:  # noqa: BLE001
                ui.call(self._recv_fail, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def _recv_progress(self, name, done, total):
        self.r_bar["value"] = 100 if not total else done * 100 // total
        self.r_msg.set(f"正在下载 {name}  {common.human(done)}/{common.human(total)}")

    def _recv_done(self, n, directory):
        self.recv_btn.configure(state="normal")
        self.r_bar["value"] = 100
        self.r_msg.set(f"完成：{n} 个文件已保存到 {directory}")

    def _recv_fail(self, msg):
        self.recv_btn.configure(state="normal")
        self.r_msg.set("失败")
        self._log_recv(f"✗ {msg}\n")
        self.messagebox.showerror("下载失败", msg)
