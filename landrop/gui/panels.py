"""Three desktop pages using the same local model and peer protocol as the CLI."""
from __future__ import annotations

import math
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

from landrop import __version__, common, remote
from landrop.server.store import OverlapError
from . import system, widgets as W
from .theme import T


class UiBridge:
    def __init__(self, root):
        self.root, self.queue, self.closed = root, queue.Queue(), False
        self._poll()

    def call(self, function, *args):
        if not self.closed:
            self.queue.put((function, args))

    def _poll(self):
        try:
            while True:
                function, args = self.queue.get_nowait()
                function(*args)
        except queue.Empty:
            pass
        if not self.closed:
            self.timer = self.root.after(50, self._poll)

    def close(self):
        self.closed = True
        self.root.after_cancel(self.timer)


def row(parent, pady=0):
    frame = W.frame(parent)
    frame.pack(fill="x", pady=pady)
    return frame


def heading(parent, text):
    W.label(parent, text, size=11, weight="bold").pack(anchor="w", pady=(T.px(16), T.px(8)))


def text_box(parent, height=4):
    widget = tk.Text(parent, height=height, wrap="word", font=T.font(10), relief="flat",
                     bd=0, padx=10, pady=9, highlightthickness=1, highlightbackground=T.c["line"],
                     bg=T.c["surface"], fg=T.c["text"], insertbackground=T.c["text"])
    widget._roles = {"bg": "surface", "fg": "text", "insertbackground": "text", "highlightbackground": "line"}
    return widget


def view_text(app, content, title="文本"):
    window = tk.Toplevel(app.root)
    window.title(title)
    window.transient(app.root)
    window.configure(bg=T.c["bg"])
    window.geometry("740x440")
    body = W.frame(window)
    body.pack(fill="both", expand=True, padx=16, pady=16)
    area = text_box(body, 16)
    area.pack(fill="both", expand=True)
    area.insert("1.0", content)
    area.configure(state="disabled")
    W.Button(body, "复制", lambda: app.copy(content), icon="copy").pack(anchor="e", pady=(10, 0))
    window.focus_set()


class Page(W.ScrollFrame):
    key = ""
    title = ""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app, self.busy, self.generation = app, False, 0
        self.wrap = W.frame(self.body)
        self.wrap.pack(fill="both", expand=True, padx=T.px(26), pady=T.px(24))
        W.label(self.wrap, self.title, size=19, weight="bold").pack(anchor="w", pady=(0, T.px(18)))

    def on_show(self):
        pass

    def on_busy(self):
        pass

    def job(self, action, done=None, failed=None):
        if self.busy:
            return
        self.busy = True
        self.generation += 1
        self.app.set_busy(self.key, True)
        self.on_busy()
        def work():
            try:
                result, error = action(), None
            except Exception as exc:
                result, error = None, exc
            finally:
                self.app.store.close()
            self.app.bridge.call(finish, result, error)
        def finish(result, error):
            self.busy = False
            self.app.set_busy(self.key, False)
            self.on_busy()
            if error:
                if failed:
                    failed(error)
                elif not isinstance(error, common.Cancelled):
                    self.app.toast.show(str(error), "error", 4500)
            elif done:
                done(result)
        threading.Thread(target=work, daemon=True).start()


class SharePage(Page):
    key, title = "share", "分享"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.share_id, self.item, self.paths = None, None, []
        self.selection = tk.StringVar()
        self.label = tk.StringVar()
        self.upload = tk.BooleanVar()
        self.public = tk.BooleanVar()
        self.enabled = tk.BooleanVar(value=True)
        self.collection = tk.StringVar()
        self.expire = tk.StringVar(value="0")
        self.service_on = tk.BooleanVar()
        self.service_status, self.address, self.code = tk.StringVar(), tk.StringVar(), tk.StringVar()
        service = row(self.wrap, (0, T.px(16)))
        self.service_toggle = W.Toggle(service, self.service_on, command=self.toggle_service)
        self.service_toggle.pack(side="left")
        W.label(service, "本机服务", weight="bold").pack(side="left", padx=10)
        W.label(service, textvariable=self.service_status, role="muted").pack(side="left")
        W.hline(self.wrap)
        select = row(self.wrap, (T.px(14), T.px(12)))
        self.chooser = W.Select(select, self.selection, [], width=350)
        self.chooser.pack(side="left", fill="x", expand=True)
        W.IconButton(select, "plus", self.new, tooltip="新分享").pack(side="left", padx=8)
        W.Button(select, "刷新", self.refresh_choices, kind="ghost", size="sm").pack(side="left")
        self.selection.trace_add("write", lambda *_: self.selected())

        self.result = W.frame(self.wrap)
        result_row = row(self.result)
        values = W.frame(result_row)
        values.pack(side="left", fill="x", expand=True)
        W.label(values, textvariable=self.address, role="muted", mono=True, wrap=390).pack(anchor="w")
        W.label(values, textvariable=self.code, mono=True, size=24, weight="bold").pack(anchor="w", pady=6)
        buttons = row(values)
        W.IconButton(buttons, "copy", self.copy_info, tooltip="复制分享信息").pack(side="left")
        W.IconButton(buttons, "external", self.open_browser, tooltip="浏览器打开分享").pack(side="left", padx=5)
        W.IconButton(buttons, "key", self.rotate, tooltip="换码，旧会话失效").pack(side="left")
        W.IconButton(buttons, "trash", self.delete, kind="danger", tooltip="删除分享，不删除磁盘文件").pack(side="left", padx=5)
        self.qr = W.QRCode(result_row, size=116)
        self.qr.pack(side="right", padx=(10, 0))
        active = row(self.result, pady=8)
        W.Toggle(active, self.enabled, command=self.toggle_enabled).pack(side="left")
        W.label(active, "启用分享").pack(side="left", padx=10)
        self.expiry_label = W.label(active, "", role="muted")
        self.expiry_label.pack(side="right")
        self.result.pack(fill="x", pady=(0, 10))

        heading(self.wrap, "名称")
        self.name_field = W.Field(self.wrap, self.label)
        self.name_field.pack(fill="x")
        heading(self.wrap, "共享文件")
        choices = row(self.wrap)
        W.Button(choices, "选择文件", self.add_files, icon="file", size="sm").pack(side="left")
        W.Button(choices, "选择文件夹", self.add_folder, icon="folder", size="sm").pack(side="left", padx=8)
        self.source_rows = W.frame(self.wrap)
        self.source_rows.pack(fill="x", pady=8)
        options = row(self.wrap, pady=8)
        self.upload_toggle = W.Toggle(options, self.upload)
        self.upload_toggle.pack(side="left")
        W.label(options, "允许上传").pack(side="left", padx=(8, 24))
        self.public_toggle = W.Toggle(options, self.public)
        self.public_toggle.pack(side="left")
        W.label(options, "公开收到的内容").pack(side="left", padx=8)
        heading(self.wrap, "收集目录")
        directory = row(self.wrap)
        self.collection_field = W.Field(directory, self.collection)
        self.collection_field.pack(side="left", fill="x", expand=True)
        W.IconButton(directory, "folder-open", self.pick_collection, tooltip="选择收集目录").pack(side="left", padx=6)
        self.actual_dir = W.label(self.wrap, "", size=9, role="muted", wrap=560)
        self.actual_dir.pack(anchor="w", pady=6)
        duration = row(self.wrap, pady=10)
        W.label(duration, "有效期（小时，0 为长期）").pack(side="left")
        W.Stepper(duration, self.expire, 0, 8760).pack(side="left", padx=12)
        W.Button(duration, "续期", self.renew, kind="ghost", size="sm").pack(side="left")
        self.expire.trace_add("write", lambda *_: setattr(self, "expire_changed", True))
        heading(self.wrap, "发布文本")
        self.text = text_box(self.wrap, 3)
        self.text.pack(fill="x")
        controls = row(self.wrap, pady=12)
        self.save_button = W.Button(controls, "开始分享", self.save, kind="primary", icon="send")
        self.save_button.pack(side="right")
        self.text_rows = W.frame(self.wrap)
        self.text_rows.pack(fill="x", pady=8)
        self.refresh_choices()
        self.poll_service()

    def on_busy(self):
        self.save_button.set_state("busy" if self.busy else "normal")
        self.service_toggle.set_enabled(not self.busy)

    def on_show(self):
        self.render_service()

    def poll_service(self):
        self.render_service()
        if not self.app.bridge.closed:
            self.service_timer = self.app.root.after(1000, self.poll_service)

    def shutdown(self):
        self.app.root.after_cancel(self.service_timer)

    def render_service(self):
        info = self.app.runtime.info
        self.service_on.set(bool(info))
        self.service_status.set(info["base"] if info else "未启动")
        self.app.set_service_state(bool(info))
        if self.item:
            settings = self.app.store.settings()
            ip = settings["ip"] or self.app.cached_ip
            ip = f"[{ip}]" if ":" in ip else ip
            base = info["base"] if info else f"http://{ip}:{settings['port']}"
            self.address.set(base)
            self.code.set(self.item["code"])
            self.qr.set(common.make_link(base, self.item["code"]))

    def refresh_choices(self, preferred=None):
        if self.busy:
            return
        shares = self.app.store.shares()
        self.chooser.set_options([("", "新分享")] + [(s["id"], s["label"] + " · " + s["code"]) for s in shares])
        target = preferred if preferred is not None else self.selection.get()
        if not target and shares:
            target = shares[0]["id"]
        self.selection.set(target)
        self.load(target)

    def selected(self):
        if self.busy:
            return
        self.load(self.selection.get())

    def new(self):
        if not self.busy:
            self.selection.set("")
            self.load("")

    def load(self, share_id):
        try:
            item = self.app.store.share(share_id) if share_id else None
        except common.CliError:
            item = None
        self.item, self.share_id = item, item["id"] if item else None
        self.form_version = item["version"] if item else None
        self.paths = [root["path"] for root in item["roots"]] if item else []
        self.label.set(item["label"] if item else "")
        self.upload.set(bool(item and item["allow_upload"]))
        self.public.set(bool(item and item["public_received"]))
        self.enabled.set(item["active"] if item else True)
        self.collection.set(item["collection_base"] if item else self.app.store.settings()["collect_dir"])
        hours = math.ceil((item["expires_at"] - item["created_at"]) / 3600) if item and item["expires_at"] else 0
        self.expire.set(str(max(0, hours)))
        self.expire_changed = False
        self.text.delete("1.0", "end")
        self.save_button.configure_text("保存更改" if item else "开始分享")
        self.render_sources()
        self.result.pack_forget()
        if item:
            self.result.pack(fill="x", after=self.chooser.master, pady=(0, 10))
            self.actual_dir.configure(text=item["collection_dir"])
            self.expiry_label.configure(text="已过期" if item["expires_at"] and item["expires_at"] <= time.time() else
                                        time.strftime("%Y-%m-%d %H:%M", time.localtime(item["expires_at"])) if item["expires_at"] else "长期")
        else:
            self.actual_dir.configure(text="")
        self.render_texts()
        self.render_service()

    def render_sources(self):
        for widget in self.source_rows.winfo_children():
            widget.destroy()
        for path in self.paths:
            item = row(self.source_rows, pady=3)
            W.Icon(item, "folder" if os.path.isdir(path) else "file", size=17).pack(side="left", padx=(0, 8))
            name = W.label(item, W.elide(path, 68), role="text2" if os.path.exists(path) else "danger", wrap=450)
            name.pack(side="left", fill="x", expand=True)
            W.Tooltip(name, path)
            W.IconButton(item, "x", lambda p=path: self.remove_path(p), tooltip="移出分享，不删除文件").pack(side="right")

    def add_paths(self, paths):
        if self.busy:
            return
        for path in paths:
            path = os.path.abspath(path)
            if path not in self.paths:
                self.paths.append(path)
        self.render_sources()

    def remove_path(self, path):
        if not self.busy:
            self.paths.remove(path)
            self.render_sources()

    def add_files(self):
        self.add_paths(filedialog.askopenfilenames(parent=self.app.root))

    def add_folder(self):
        path = filedialog.askdirectory(parent=self.app.root)
        if path:
            self.add_paths([path])

    def pick_collection(self):
        path = filedialog.askdirectory(parent=self.app.root, initialdir=self.collection.get() or None)
        if path:
            self.collection.set(path)

    def save(self, confirmed=False):
        if self.busy:
            return
        text = self.text.get("1.0", "end-1c")
        if not self.share_id and not self.paths and not self.upload.get() and not text.strip():
            self.app.toast.show("尚未选择文件或提供收集/文本内容", "error")
            return
        values = {"paths": list(self.paths), "label": self.label.get(), "allow_upload": self.upload.get(),
                  "public_received": self.public.get(), "collection_base": self.collection.get(),
                  "enabled": self.enabled.get(), "text": text if text.strip() else None,
                  "expected_version": self.form_version, "confirm_overlap": confirmed}
        try:
            if not self.item or self.expire_changed:
                hours = float(self.expire.get())
                if not math.isfinite(hours) or hours < 0:
                    raise ValueError
                values["expires_at"] = time.time() + hours * 3600 if hours else None
        except ValueError:
            self.app.toast.show("有效期须为非负小时数", "error")
            return
        share_id = self.share_id
        def action():
            result = self.app.store.save_share(share_id, **values)
            if not share_id:
                self.app.runtime.start()
            return result
        def failed(error):
            if isinstance(error, OverlapError) and messagebox.askyesno("确认目录共享", str(error), parent=self.app.root):
                self.save(True)
            else:
                self.app.toast.show(str(error), "error", 4500)
                self.refresh_choices(share_id)
        self.job(action, lambda item: self.refresh_choices(item["id"]), failed)

    def renew(self):
        if self.item:
            self.expire_changed = True
            self.save()

    def toggle_service(self):
        start = self.service_on.get()
        if not start and self.app.runtime.active_transfers and not messagebox.askokcancel(
                "停止服务", "正在传输，停止会中断。完整文件保留。", parent=self.app.root):
            self.service_on.set(True)
            return
        self.job(self.app.runtime.start if start else self.app.runtime.stop, lambda _: self.render_service(),
                 lambda error: (self.render_service(), self.app.toast.show(str(error), "error")))

    def toggle_enabled(self):
        if not self.item:
            return
        share_id, enabled = self.share_id, self.enabled.get()
        if not enabled and self.app.runtime.active_transfers and not messagebox.askokcancel(
                "停用分享", "正在传输，停用会中断这份分享。", parent=self.app.root):
            self.enabled.set(True)
            return
        self.job(lambda: self.app.store.set_enabled(share_id, enabled),
                 lambda item: self.refresh_choices(item["id"]),
                 lambda error: (self.enabled.set(self.item["active"]), self.app.toast.show(str(error), "error")))

    def rotate(self):
        if self.item and messagebox.askokcancel("更换授权码", "旧码、链接和登录会话立即失效，正在传输的任务会中断。", parent=self.app.root):
            self.job(lambda: self.app.store.rotate(self.share_id), lambda item: self.refresh_choices(item["id"]))

    def delete(self):
        if self.item and messagebox.askokcancel("删除分享", "将移除配置和关联文本，磁盘文件与收集目录保留。", parent=self.app.root):
            self.job(lambda: self.app.store.delete_share(self.share_id), lambda _: self.refresh_choices(""))

    def copy_info(self):
        if self.item:
            self.app.copy(f"访问地址: {self.address.get()}\n授权码: {self.item['code']}\n"
                          f"分享链接: {common.make_link(self.address.get(), self.item['code'])}")

    def open_browser(self):
        if self.item:
            system.open_url(common.make_link(self.address.get(), self.item["code"]))

    def render_texts(self):
        for widget in self.text_rows.winfo_children():
            widget.destroy()
        if not self.item:
            return
        texts = self.app.store.texts(self.share_id, local=True)
        if texts:
            controls = row(self.text_rows, pady=8)
            W.label(controls, f"文本 · {len(texts)}", weight="bold").pack(side="left")
            W.Button(controls, "清空", self.clear_texts, kind="ghost", size="sm", icon="trash").pack(side="right")
        for index, item in enumerate(texts, 1):
            frame = row(self.text_rows, pady=3)
            W.label(frame, f"文本 {index} · {common.human(item['size'])}" + (" · 收到" if item["received"] else ""), role="text2").pack(side="left")
            W.IconButton(frame, "eye", lambda i=item: self.show_text(i), tooltip="查看文本").pack(side="right")

    def show_text(self, item):
        share_id = self.share_id
        self.job(lambda: self.app.store.text(share_id, item["id"], local=True), lambda text: view_text(self.app, text))

    def clear_texts(self):
        if messagebox.askokcancel("清空文本", "将删除这份分享保存的全部文本。", parent=self.app.root):
            self.job(lambda: self.app.store.clear_texts(self.share_id), lambda _: self.render_texts())


class ReceivePage(Page):
    key, title = "receive", "接收"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.address, self.code = tk.StringVar(), tk.StringVar()
        self.dir = tk.StringVar(value=app.settings["recv_dir"])
        self.status = tk.StringVar(value="未连接")
        self.client, self.files, self.picks = None, [], {}
        self.cancel_event = threading.Event()
        heading(self.wrap, "地址或便捷链接")
        address = row(self.wrap)
        self.address_field = W.Field(address, self.address, mono=True)
        self.address_field.pack(side="left", fill="x", expand=True)
        W.IconButton(address, "paste", self.paste, tooltip="粘贴分享信息").pack(side="left", padx=6)
        authorization = row(self.wrap, pady=10)
        W.label(authorization, "授权码").pack(side="left", padx=(0, 10))
        self.code_field = W.Field(authorization, self.code, mono=True, width=150)
        self.code_field.pack(side="left")
        self.connect_button = W.Button(authorization, "连接", self.connect, kind="primary", icon="arrow-right")
        self.connect_button.pack(side="right")
        self.address_field.entry.bind("<Return>", lambda _: self.connect())
        self.code_field.entry.bind("<Return>", lambda _: self.connect())
        heading(self.wrap, "保存到")
        directory = row(self.wrap)
        W.Field(directory, self.dir).pack(side="left", fill="x", expand=True)
        W.IconButton(directory, "folder-open", self.pick_dir, tooltip="选择保存目录").pack(side="left", padx=6)
        W.IconButton(directory, "external", self.open_dir, tooltip="打开保存目录").pack(side="left")
        self.progress = W.Progress(self.wrap)
        self.progress.pack(fill="x", pady=(18, 8))
        W.label(self.wrap, textvariable=self.status, role="muted", wrap=560).pack(anchor="w")
        controls = row(self.wrap, pady=12)
        self.receive_button = W.Button(controls, "接收所选", self.receive, kind="primary", icon="download")
        self.receive_button.pack(side="right")
        self.cancel_button = W.Button(controls, "取消", self.cancel, icon="x")
        self.cancel_button.pack(side="right", padx=8)
        self.all_selected = tk.BooleanVar(value=True)
        W.Check(controls, self.all_selected, command=self.select_all).pack(side="left")
        W.label(controls, "全选").pack(side="left", padx=8)
        self.file_rows = W.frame(self.wrap)
        self.file_rows.pack(fill="x")
        self.text_rows = W.frame(self.wrap)
        self.text_rows.pack(fill="x", pady=10)
        self.submit_frame = W.frame(self.wrap)
        heading(self.submit_frame, "投递到这份分享")
        self.submit_text = text_box(self.submit_frame, 3)
        self.submit_text.pack(fill="x")
        submit = row(self.submit_frame, pady=8)
        self.submit_button = W.Button(submit, "发送文本", self.send_text, icon="send")
        self.submit_button.pack(side="right")
        self.upload_button = W.Button(submit, "上传文件", self.upload_files, icon="upload")
        self.upload_button.pack(side="left")
        self.history_rows = W.frame(self.wrap)
        self.history_rows.pack(fill="x", pady=18)
        self.render_history()
        self.on_busy()

    def on_busy(self):
        self.connect_button.set_state("disabled" if self.busy else "normal")
        self.receive_button.set_state("disabled" if self.busy or not self.files else "normal")
        self.cancel_button.set_state("normal" if self.busy else "disabled")
        self.submit_button.set_state("disabled" if self.busy else "normal")
        self.upload_button.set_state("disabled" if self.busy else "normal")
        self.address_field.set_enabled(not self.busy)
        self.code_field.set_enabled(not self.busy)

    def on_show(self):
        self.dir.set(self.app.store.settings()["recv_dir"])
        self.render_history()
        if not self.address.get() and not self.busy:
            try:
                base, code = common.parse_target(self.app.root.clipboard_get())
                self.address.set(base)
                self.code.set(code)
            except (tk.TclError, common.CliError):
                pass

    def paste(self):
        try:
            value = self.app.root.clipboard_get()
            try:
                base, code = common.parse_target(value)
                self.address.set(base)
                self.code.set(code)
            except common.CliError:
                self.address.set(value.strip())
                self.code.set("")
        except tk.TclError:
            self.app.toast.show("剪贴板为空", "info")

    def connect(self):
        address, code = self.address.get(), self.code.get()
        self.status.set("连接中…")
        self.job(lambda: remote.list_remote(address, code, data_dir=self.app.store.data_dir), self.connected, self.failed)

    def connected(self, result):
        self.client, self.files = result
        self.picks = {item["id"]: tk.BooleanVar(value=True) for item in self.files}
        self.all_selected.set(True)
        for frame in (self.file_rows, self.text_rows):
            for widget in frame.winfo_children():
                widget.destroy()
        for item in self.files:
            frame = row(self.file_rows, pady=5)
            W.Check(frame, self.picks[item["id"]]).pack(side="left")
            W.label(frame, W.elide(item["name"], 64), wrap=440).pack(side="left", fill="x", expand=True, padx=10)
            W.label(frame, common.human(item["size"]), role="muted", size=9).pack(side="right")
        for index, item in enumerate(self.client.content["texts"], 1):
            frame = row(self.text_rows, pady=4)
            W.label(frame, f"文本 {index} · {common.human(item['size'])}").pack(side="left")
            W.IconButton(frame, "eye", lambda i=item: self.read_text(i), tooltip="查看和复制文本").pack(side="right")
        self.submit_frame.pack_forget()
        if self.client.content["share"]["allow_upload"]:
            self.submit_frame.pack(fill="x", before=self.history_rows)
        self.status.set(self.client.content["share"]["label"] + f" · {len(self.files)} 个文件")
        self.progress.set(0)
        self.on_busy()

    def select_all(self):
        for value in self.picks.values():
            value.set(self.all_selected.get())

    def pick_dir(self):
        path = filedialog.askdirectory(parent=self.app.root, initialdir=self.dir.get() or None)
        if path:
            self.dir.set(path)
            self.app.store.configure(recv_dir=path)

    def open_dir(self):
        if os.path.isdir(self.dir.get()):
            system.open_path(self.dir.get())

    def _progress(self, generation, name, done, total, started):
        if generation != self.generation or not self.busy:
            return
        elapsed = max(0.1, time.monotonic() - started)
        self.progress.set(done / total if total else 1)
        self.status.set(f"{W.elide(name, 45)} · {common.human(done)}/{common.human(total)} · {common.human(done / elapsed)}/s")

    def progress_callback(self):
        started, last = time.monotonic(), [0.0]
        generation = self.generation + 1
        def callback(name, done, total):
            if self.cancel_event.is_set():
                raise common.Cancelled("已取消")
            if time.monotonic() - last[0] > 0.08 or done == total:
                last[0] = time.monotonic()
                self.app.bridge.call(self._progress, generation, name, done, total, started)
        return callback

    def receive(self):
        if not self.client or self.busy:
            return
        selected = [item for item in self.files if self.picks[item["id"]].get()]
        if not selected:
            self.app.toast.show("未选择文件", "info")
            return
        directory = os.path.abspath(os.path.expanduser(self.dir.get()))
        client = self.client
        self.cancel_event.clear()
        progress = self.progress_callback()
        self.status.set("接收中…")
        def action():
            os.makedirs(directory, exist_ok=True)
            for item in selected:
                remote.download_file(client, item, directory, on_progress=progress,
                                     should_cancel=self.cancel_event.is_set)
            self.app.store.record_receive(len(selected), directory)
            return directory
        def done(path):
            self.progress.set(1)
            self.status.set("已保存到 " + path)
            self.render_history()
            self.app.notify("接收完成", f"{len(selected)} 个文件")
        self.job(action, done, self.failed)

    def upload_files(self):
        if not self.client or self.busy:
            return
        paths = filedialog.askopenfilenames(parent=self.app.root)
        if not paths:
            return
        client = self.client
        self.cancel_event.clear()
        progress = self.progress_callback()
        def action():
            for path in paths:
                remote.upload_file(client, path, progress, should_cancel=self.cancel_event.is_set)
        self.status.set("上传中…")
        self.job(action, lambda _: self.status.set("文件已投递"), self.failed)

    def send_text(self):
        if not self.client:
            return
        content, client = self.submit_text.get("1.0", "end-1c"), self.client
        self.job(lambda: client.submit_text(content),
                 lambda _: (self.submit_text.delete("1.0", "end"), self.status.set("文本已提交")), self.failed)

    def read_text(self, item):
        client = self.client
        self.job(lambda: client.text(item["id"]), lambda text: view_text(self.app, text), self.failed)

    def cancel(self):
        self.cancel_event.set()
        self.status.set("正在取消…")

    def shutdown(self):
        self.cancel_event.set()

    def failed(self, error):
        self.status.set(str(error))
        self.progress.stop()
        if not isinstance(error, common.Cancelled):
            self.app.toast.show(str(error), "error", 4500)
        if getattr(error, "status", None) in (401, 403):
            self.client, self.files, self.picks = None, [], {}
            for frame in (self.file_rows, self.text_rows):
                for widget in frame.winfo_children():
                    widget.destroy()
            self.submit_frame.pack_forget()
        self.on_busy()

    def render_history(self):
        for widget in self.history_rows.winfo_children():
            widget.destroy()
        history = self.app.store.settings()["receive_history"]
        if history:
            controls = row(self.history_rows)
            W.label(controls, "最近接收", weight="bold").pack(side="left")
            W.Button(controls, "清空记录", self.clear_history, size="sm", kind="ghost").pack(side="right")
        for item in history:
            frame = row(self.history_rows, pady=5)
            timestamp = time.strftime("%m-%d %H:%M", time.localtime(item["time"]))
            W.label(frame, f"{timestamp} · {item['count']} 个文件 · {W.elide(item['directory'], 42)}", size=9,
                    role="muted", wrap=500).pack(side="left", fill="x", expand=True)
            W.IconButton(frame, "folder-open", lambda p=item["directory"]: system.open_path(p) if os.path.isdir(p) else None,
                         tooltip="打开保存目录").pack(side="right")

    def clear_history(self):
        self.app.store.configure(receive_history=[])
        self.render_history()


class SettingsPage(Page):
    key, title = "settings", "设置"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.theme = tk.StringVar(value=app.settings["theme"])
        W.Segmented(self.wrap, [("system", "跟随系统"), ("light", "浅色"), ("dark", "深色")],
                    self.theme, command=lambda: self.app.apply_theme(self.theme.get())).pack(anchor="w")
        self.variables = {}
        for key, label in (("collect_dir", "默认收集目录"), ("recv_dir", "默认接收目录")):
            heading(self.wrap, label)
            variable = self.variables[key] = tk.StringVar(value=app.settings[key])
            frame = row(self.wrap)
            W.Field(frame, variable).pack(side="left", fill="x", expand=True)
            W.IconButton(frame, "folder-open", lambda v=variable: self.pick(v), tooltip="选择目录").pack(side="left", padx=6)
        heading(self.wrap, "高级设置")
        for key, label in (("ip", "访问地址（留空自动）"), ("port", "端口"),
                           ("text_preview_mib", "文本预览上限（MiB）"), ("image_preview_mib", "图片预览上限（MiB）")):
            frame = row(self.wrap, pady=5)
            W.label(frame, label).pack(side="left")
            variable = self.variables[key] = tk.StringVar(value=str(app.settings[key]))
            if key == "ip":
                W.Field(frame, variable, width=240, mono=True).pack(side="right")
            else:
                W.Stepper(frame, variable, 1, 65535 if key == "port" else 1024, width=100).pack(side="right")
        self.save_button = W.Button(self.wrap, "保存设置", self.save, icon="check", kind="primary")
        self.save_button.pack(anchor="e", pady=14)
        heading(self.wrap, "命令行工具")
        self.cli_state = W.label(self.wrap, "", role="muted", wrap=560)
        self.cli_state.pack(anchor="w")
        self.cli_button = W.Button(self.wrap, "加入 PATH", self.toggle_cli, icon="terminal", size="sm")
        self.cli_button.pack(anchor="w", pady=8)
        diagnostics = row(self.wrap, pady=14)
        W.Button(diagnostics, "打开数据目录", lambda: system.open_path(app.store.data_dir), icon="folder-open", size="sm").pack(side="left")
        W.Button(diagnostics, "诊断日志", lambda: system.open_path(os.path.join(app.store.data_dir, "diagnostics.log")),
                 icon="file", size="sm").pack(side="left", padx=8)
        W.label(self.wrap, "LAN Drop " + __version__, role="muted", size=9).pack(anchor="w", pady=8)
        self.refresh_cli()

    def on_show(self):
        values = self.app.store.settings()
        for key, variable in self.variables.items():
            variable.set(str(values[key]))
        self.refresh_cli()

    def on_busy(self):
        self.save_button.set_state("busy" if self.busy else "normal")

    def pick(self, variable):
        value = filedialog.askdirectory(parent=self.app.root, initialdir=variable.get() or None)
        if value:
            variable.set(value)

    def save(self):
        if self.busy:
            return
        values = {key: variable.get().strip() for key, variable in self.variables.items()}
        old = self.app.store.settings()
        changed_network = str(old["port"]) != values["port"] or old["ip"] != values["ip"]
        running = self.app.runtime.running
        if changed_network and running and not messagebox.askokcancel("重启本机服务", "网络设置变化，需要重启服务；正在传输会中断。", parent=self.app.root):
            return
        def action():
            # Validate first so an invalid setting cannot interrupt the live service.
            updated = self.app.store.configure(**values)
            if changed_network and running:
                self.app.runtime.stop()
                self.app.runtime.start()
            return updated
        self.job(action, lambda settings: (setattr(self.app, "settings", settings),
                                          self.app.share.render_service(), self.app.toast.show("设置已保存")))

    def refresh_cli(self):
        status = system.cli_status()
        self.cli_state.configure(text=status["location"] or
                                 ("命令行快捷入口未链接" if status["available"] else "当前运行方式无独立命令行入口"))
        self.cli_button.configure_text("移出 PATH" if status["installed"] else "加入 PATH")
        self.cli_button.set_state("normal" if status["can_toggle"] else "disabled")

    def toggle_cli(self):
        status = system.cli_status()
        action = system.uninstall_cli if status["installed"] else system.install_cli
        self.job(action, lambda result: (self.refresh_cli(), self.app.toast.show(result)))
