# -*- coding: utf-8 -*-
"""桌面启动器里的「发送 / 接收」页（Windows / macOS 使用，Linux 用命令行 landrop.py）。

界面只做三件事：选文件 → 生成文件码与取件命令 → 用文件码取件。
真正的上传、校验、下载都调用 landrop.py 里的 ``send_files`` / ``fetch_files``，
与命令行行为完全一致；耗时操作放在后台线程，通过队列回到界面线程更新进度。
"""
from __future__ import annotations

import os
import queue
import sys
import threading

from landrop import common as core_common
from landrop import remote as core_remote


def open_path(path: str):
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        import subprocess
        subprocess.Popen(["open", path])
    else:
        import subprocess
        subprocess.Popen(["xdg-open", path])


class Worker:
    """后台线程 + 队列：worker 里只往队列放事件，界面线程轮询后更新控件。"""

    def __init__(self, root):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self._poll()

    def run(self, fn, on_progress, on_ok, on_err):
        if self.busy:
            return
        self.busy = True

        def target():
            try:
                res = fn(lambda *a: self.q.put(("p", on_progress, a)))
                self.q.put(("ok", on_ok, (res,)))
            except core_common.CliError as exc:
                self.q.put(("err", on_err, (str(exc),)))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("err", on_err, (f"{type(exc).__name__}: {exc}",)))

        threading.Thread(target=target, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, cb, args = self.q.get_nowait()
                if kind != "p":
                    self.busy = False
                cb(*args)
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
        self.worker = Worker(root)
        self.files: list[str] = []
        self.last_grant = ""
        self.font = ("Consolas", 10) if sys.platform.startswith("win") else ("Menlo", 11)

        self.send_tab = ttk.Frame(notebook, padding=14)
        self.recv_tab = ttk.Frame(notebook, padding=14)
        notebook.add(self.send_tab, text="发送文件")
        notebook.add(self.recv_tab, text="接收文件")
        self._build_send(default_url)
        self._build_recv()

    # ------------------------------------------------------------ 发送页
    def _build_send(self, default_url):
        tk, ttk = self.tk, self.ttk
        f = self.send_tab
        f.columnconfigure(1, weight=1)
        pad = {"padx": 8, "pady": 4}

        ttk.Label(f, text="服务地址").grid(row=0, column=0, sticky="w", **pad)
        self.s_url = tk.StringVar(value=os.environ.get("LANDROP_URL") or default_url)
        ttk.Entry(f, textvariable=self.s_url).grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(f, text="管理员密钥").grid(row=1, column=0, sticky="w", **pad)
        try:
            key = os.environ.get("LANDROP_ADMIN_KEY") or core_remote.read_admin_key()
        except core_common.CliError:
            key = ""
        self.s_key = tk.StringVar(value=key)
        ttk.Entry(f, textvariable=self.s_key, show="•").grid(row=1, column=1, columnspan=2,
                                                             sticky="ew", **pad)

        ttk.Label(f, text="要发送的文件").grid(row=2, column=0, sticky="nw", **pad)
        box = ttk.Frame(f)
        box.grid(row=2, column=1, sticky="nsew", **pad)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(box, height=6, selectmode="extended")
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box, command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)
        f.rowconfigure(2, weight=1)
        btns = ttk.Frame(f)
        btns.grid(row=2, column=2, sticky="n", **pad)
        ttk.Button(btns, text="添加文件…", command=self.add_files).pack(fill="x")
        ttk.Button(btns, text="移除选中", command=self.remove_files).pack(fill="x", pady=4)
        ttk.Button(btns, text="清空", command=self.clear_files).pack(fill="x")

        opt = ttk.Frame(f)
        opt.grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(opt, text="有效期（小时，0=永久）").pack(side="left")
        self.s_expire = tk.StringVar(value="24")
        ttk.Spinbox(opt, from_=0, to=8760, width=6, textvariable=self.s_expire).pack(
            side="left", padx=6)

        self.send_btn = ttk.Button(f, text="上传并生成文件码", command=self.do_send)
        self.send_btn.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        self.s_bar = ttk.Progressbar(f, maximum=100)
        self.s_bar.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
        self.s_msg = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.s_msg).grid(row=6, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(f, text="文件码").grid(row=7, column=0, sticky="w", **pad)
        self.s_code = tk.StringVar()
        ttk.Entry(f, textvariable=self.s_code, state="readonly", font=self.font).grid(
            row=7, column=1, sticky="ew", **pad)
        self.copy_code_btn = ttk.Button(f, text="复制文件码", command=self.copy_code,
                                        state="disabled")
        self.copy_code_btn.grid(row=7, column=2, sticky="ew", **pad)
        ttk.Label(f, text="取件命令").grid(row=8, column=0, sticky="w", **pad)
        self.s_cmd = tk.StringVar()
        ttk.Entry(f, textvariable=self.s_cmd, state="readonly", font=self.font).grid(
            row=8, column=1, sticky="ew", **pad)
        self.copy_cmd_btn = ttk.Button(f, text="复制命令", command=self.copy_cmd, state="disabled")
        self.copy_cmd_btn.grid(row=8, column=2, sticky="ew", **pad)
        self.revoke_btn = ttk.Button(f, text="撤销这个文件码", command=self.do_revoke,
                                     state="disabled")
        self.revoke_btn.grid(row=9, column=1, sticky="w", **pad)

    def set_local_service(self, url: str, key: str):
        """服务在本窗口启动后，自动填好发送页的地址与密钥。"""
        self.s_url.set(url)
        if key:
            self.s_key.set(key)

    def add_files(self):
        for p in self.filedialog.askopenfilenames(title="选择要发送的文件（可多选）"):
            p = os.path.abspath(p)
            if p not in self.files:
                self.files.append(p)
                self.listbox.insert("end", f"{os.path.basename(p)}   ({core_common.human(os.path.getsize(p))})")

    def remove_files(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.files[i]

    def clear_files(self):
        self.listbox.delete(0, "end")
        self.files.clear()

    def _set_bar(self, name, done, total):
        self.s_bar["value"] = 100 if not total else done * 100 // total
        self.s_msg.set(f"正在上传 {name}  {core_common.human(done)}/{core_common.human(total)}")

    def do_send(self):
        if not self.files:
            self.messagebox.showinfo("没有文件", "请先点「添加文件…」选择一个或多个文件。")
            return
        try:
            expire = float(self.s_expire.get() or 24)
            assert expire >= 0
        except (ValueError, AssertionError):
            self.messagebox.showerror("有效期无效", "有效期必须是不小于 0 的数字。")
            return
        url, key, files = self.s_url.get().strip(), self.s_key.get().strip(), list(self.files)
        if not key:
            self.messagebox.showerror("缺少密钥", "请填写管理员密钥（本机服务启动后会自动填入）。")
            return
        self.send_btn.configure(state="disabled")
        self.s_code.set("")
        self.s_cmd.set("")
        self.s_msg.set("连接中…")

        def ok(res):
            self.send_btn.configure(state="normal")
            self.s_bar["value"] = 100
            self.s_code.set(res["code"])
            self.s_cmd.set(res["command"])
            self.last_grant = res["grant_id"]
            for b in (self.copy_code_btn, self.copy_cmd_btn, self.revoke_btn):
                b.configure(state="normal")
            self.s_msg.set(f"完成：{res['count']} 个文件，{core_common.human(res['bytes'])}。"
                           "把文件码或取件命令发给对方即可。")

        def err(msg):
            self.send_btn.configure(state="normal")
            self.s_msg.set("失败")
            self.messagebox.showerror("发送失败", msg)

        self.worker.run(
            lambda cb: core_remote.send_files(url, key, files, expire, "", "", cb),
            self._set_bar, ok, err)

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.s_msg.set("已复制到剪贴板")

    def copy_code(self):
        self._copy(self.s_code.get())

    def copy_cmd(self):
        self._copy(self.s_cmd.get())

    def do_revoke(self):
        if not self.last_grant or not self.messagebox.askyesno("撤销", "撤销后对方将无法再用这个文件码取件。确定？"):
            return
        url, key, gid = self.s_url.get().strip(), self.s_key.get().strip(), self.last_grant

        def run(cb):
            cli = core_remote.Client(url)
            cli.login(key)
            cli.request("POST", "/api/grants/revoke", {"id": gid})
            return gid

        def ok(_):
            self.s_msg.set("文件码已撤销")
            self.revoke_btn.configure(state="disabled")

        self.worker.run(run, lambda *a: None, ok,
                        lambda m: self.messagebox.showerror("撤销失败", m))

    # ------------------------------------------------------------ 接收页
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
            self.r_code.set(core_common.extract_code(self.root.clipboard_get()))
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

    def _log(self, text):
        self.r_log.configure(state="normal")
        self.r_log.insert("end", text)
        self.r_log.see("end")
        self.r_log.configure(state="disabled")

    def do_recv(self):
        code = core_common.extract_code(self.r_code.get())
        directory = os.path.expanduser(self.r_dir.get().strip())
        if not code or not directory:
            self.messagebox.showinfo("信息不全", "请填写文件码和保存目录。")
            return
        force = self.r_force.get()
        self.r_code.set(code)
        self.recv_btn.configure(state="disabled")
        self.r_bar["value"] = 0
        self.r_msg.set("连接中…")

        def prog(name, done, total):
            self.r_bar["value"] = 100 if not total else done * 100 // total
            self.r_msg.set(f"正在下载 {name}  {core_common.human(done)}/{core_common.human(total)}")

        def run(cb):
            return core_remote.fetch_files(code, directory, None, force, cb,
                                    lambda item, target: cb("done", item["size"], target))

        def progress(*a):
            if a[0] == "done":
                self._log(f"✓ {a[2]}  ({core_common.human(a[1])}，已校验 SHA-256)\n")
            else:
                prog(*a)

        def ok(saved):
            self.recv_btn.configure(state="normal")
            self.r_bar["value"] = 100
            self.r_msg.set(f"完成：{len(saved)} 个文件已保存到 {directory}")

        def err(msg):
            self.recv_btn.configure(state="normal")
            self.r_msg.set("失败")
            self._log(f"✗ {msg}\n")
            self.messagebox.showerror("下载失败", msg)

        self.worker.run(run, progress, ok, err)
