# -*- coding: utf-8 -*-
"""桌面窗口的各个页面：发送 / 接收 / 收集服务 / 设置。

交互参考同类工具：LocalSend 的侧边栏 + 卡片布局，GNOME Warp / croc 的「文件码 + 二维码」，
PairDrop 的「浏览器就能收」。真正的传输都调用 ``landrop.direct`` / ``landrop.remote``，
与命令行行为完全一致；耗时操作在后台线程里，经 UiBridge 回到界面线程更新控件。
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox
from urllib.parse import quote

from landrop import __version__, common, direct, remote

from . import system
from . import widgets as W
from .theme import T, file_kind


CODE_RE = re.compile(r"(?:https?://)?[\w.\-\[\]:]+:\d+/[A-Za-z0-9_-]{8,}")


class Cancelled(Exception):
    """用户点了取消：从进度回调里抛出，打断上传 / 下载 / 校验。"""


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
                try:
                    fn(*args)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        except queue.Empty:
            pass
        try:
            self.root.after(60, self._poll)
        except tk.TclError:
            pass


def run_bg(fn):
    threading.Thread(target=fn, daemon=True).start()


def _speed(done: int, started: float) -> str:
    dt = time.time() - started
    return f"{common.human(done / dt)}/s" if dt > 0.5 and done else ""


# =========================================================================== 页面骨架
class Page(W.ScrollFrame):
    title = ""
    subtitle = ""
    icon = "file"

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.wrap = W.frame(self.body)
        self.wrap.pack(fill="both", expand=True, padx=T.px(36), pady=(T.px(30), T.px(36)))
        head = W.frame(self.wrap)
        head.pack(fill="x", pady=(0, T.px(20)))
        W.label(head, self.title, size=19, weight="bold").pack(anchor="w")
        self.subtitle_label = W.label(head, self.subtitle, role="muted", size=10)
        self.subtitle_label.pack(anchor="w", pady=(T.px(4), 0))

    def card(self, pady=(0, 14), **kw) -> W.Card:
        c = W.Card(self.wrap, **kw)
        c.pack(fill="x", pady=(T.px(pady[0]), T.px(pady[1])))
        return c

    def on_show(self):
        pass


def row(parent, bg="surface", pady=0) -> tk.Frame:
    f = W.frame(parent, bg=bg)
    f.pack(fill="x", pady=pady)
    return f


def field_row(parent, title, hint=None, bg="surface"):
    """左侧标题（可带说明），右侧放控件的一行；返回右侧容器。"""
    r = row(parent, bg, pady=(T.px(6), T.px(6)))
    left = W.frame(r, bg=bg)
    left.pack(side="left", fill="y")
    W.label(left, title, size=10, weight="bold").pack(anchor="w")
    if hint:
        W.label(left, hint, role="muted", size=9).pack(anchor="w")
    right = W.frame(r, bg=bg)
    right.pack(side="right")
    return right


# =========================================================================== 发送
class SendPage(Page):
    title = "发送"
    subtitle = "选好文件，生成文件码发给对方：对方粘贴文件码、运行 landrop get，或用手机扫码都能取件。"
    icon = "send"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        s = app.settings
        self.files: list[str] = []
        self._stats: dict[str, tuple[int, int]] = {}     # 路径 → (文件数, 字节数)，大文件夹只遍历一次
        self.sender: direct.DirectSender | None = None
        self.busy = False
        self._cancel = False
        self.mode = tk.StringVar(value=s.get("send_mode", "direct"))
        self.timeout = tk.StringVar(value=str(s.get("timeout", 30)))
        self.receivers = tk.StringVar(value=str(s.get("receivers", 1)))
        self.address = tk.StringVar(value="auto")
        self.url = tk.StringVar(value=os.environ.get("LANDROP_URL") or s.get("server_url") or "")
        try:
            key = os.environ.get("LANDROP_ADMIN_KEY") or remote.read_admin_key()
        except common.CliError:
            key = ""
        self.key = tk.StringVar(value=key)
        self.expire = tk.StringVar(value=str(s.get("expire", 24)))
        self.max_dl = tk.StringVar(value=str(s.get("max_downloads", 1)))
        self.code = tk.StringVar()
        self.command = tk.StringVar()
        self.web_url = tk.StringVar()
        self.status = tk.StringVar()
        self.ips: list[str] = []

        self.compose = W.frame(self.wrap)
        self.share = W.frame(self.wrap)
        self.compose.pack(fill="x")
        self._build_compose()
        self._build_share()
        run_bg(self._load_ips)

    # ------------------------------------------------------------ 编辑
    def _build_compose(self):
        c = W.Card(self.compose, padding=26)
        c.pack(fill="x", pady=(0, T.px(14)))
        b = c.body
        self.drop_big = W.frame(b, bg="surface")      # 已选文件后收起，只留按钮
        self.drop_big.pack(fill="x")
        W.Icon(self.drop_big, "upload", size=24, color="accent_text", tile="accent_soft", tile_size=52,
               radius=16).pack(pady=(T.px(4), T.px(12)))
        W.label(self.drop_big, "添加要发送的文件", size=13, weight="bold", anchor="center").pack()
        self.drop_hint = W.label(self.drop_big, "可以多选文件，也可以发送整个文件夹（保留目录结构）",
                                 role="muted", anchor="center")
        self.drop_hint.pack(pady=(T.px(4), T.px(16)))
        btns = W.frame(b, bg="surface")
        btns.pack()
        self.drop_btns = btns
        W.Button(btns, "选择文件", self.add_files, kind="primary", icon="file").pack(side="left")
        W.Button(btns, "选择文件夹", self.add_folder, kind="secondary", icon="folder").pack(
            side="left", padx=(T.px(10), 0))
        self.drop_card = c

        # 已选列表
        self.list_card = W.Card(self.compose, padding=18)
        lb = self.list_card.body
        head = row(lb)
        self.count_label = W.label(head, "", size=11, weight="bold")
        self.count_label.pack(side="left")
        W.Button(head, "清空", self.clear_files, kind="ghost", size="sm", icon="trash").pack(side="right")
        self.rows = W.frame(lb, bg="surface")
        self.rows.pack(fill="x", pady=(T.px(8), 0))

        # 发送方式
        oc = W.Card(self.compose, padding=20)
        oc.pack(fill="x", pady=(0, T.px(14)))
        self.options_card = oc
        ob = oc.body
        top = row(ob)
        W.label(top, "发送方式", size=11, weight="bold").pack(side="left")
        self.mode_seg = W.Segmented(top, [("direct", "直连"), ("server", "经服务器")], self.mode,
                                    command=self._mode_changed)
        self.mode_seg.pack(side="right")
        self.mode_hint = W.label(ob, "", role="muted", wrap=620)
        self.mode_hint.pack(anchor="w", pady=(T.px(8), T.px(6)))
        W.hline(ob, pady=(T.px(6), T.px(4)))

        self.direct_opts = W.frame(ob, bg="surface")
        W.Stepper(field_row(self.direct_opts, "无人取件自动结束", "分钟"), self.timeout, 1, 1440).pack()
        W.Stepper(field_row(self.direct_opts, "接收者数量", "这么多人各取完一次后结束"),
                  self.receivers, 1, 99).pack()
        self.addr_select = W.Select(field_row(self.direct_opts, "写进文件码的地址",
                                              "多网卡 / VPN 时，选对方能访问到的那个"),
                                    self.address, [("auto", "自动")], width=240)
        self.addr_select.pack()

        self.server_opts = W.frame(ob, bg="surface")
        W.label(self.server_opts, "服务地址", size=10, weight="bold").pack(anchor="w", pady=(T.px(6), T.px(4)))
        W.Field(self.server_opts, self.url, placeholder="http://192.168.1.5:8000").pack(fill="x")
        W.label(self.server_opts, "管理员密钥", size=10, weight="bold").pack(anchor="w", pady=(T.px(10), T.px(4)))
        keyrow = row(self.server_opts)
        self.key_field = W.Field(keyrow, self.key, placeholder="在服务端的 admin-key.txt 或启动日志里", show="•")
        self.key_field.pack(side="left", fill="x", expand=True)
        W.IconButton(keyrow, "eye", self._toggle_key, size=40, kind="secondary",
                     tooltip="显示 / 隐藏").pack(side="left", padx=(T.px(8), 0))
        self.local_hint = W.label(self.server_opts, "", role="accent_text", size=9)
        self.local_hint.pack(anchor="w", pady=(T.px(6), 0))
        W.Stepper(field_row(self.server_opts, "有效期", "小时，0 = 永不过期"), self.expire, 0, 8760).pack()
        W.Stepper(field_row(self.server_opts, "可取次数", "0 = 不限；默认 1 次即失效"), self.max_dl, 0, 1000).pack()

        act = row(self.compose, bg="bg")
        self.send_btn = W.Button(act, "生成文件码", self.send, kind="primary", icon="send", size="lg")
        self.send_btn.pack(side="right")
        self.compose_msg = W.label(act, "", role="muted")
        self.compose_msg.pack(side="left")
        self._mode_changed()
        self._refresh_list()

    def _toggle_key(self):
        e = self.key_field.entry
        e.configure(show="" if e.cget("show") else "•")

    def _mode_changed(self):
        direct_mode = self.mode.get() == "direct"
        self.direct_opts.pack_forget()
        self.server_opts.pack_forget()
        (self.direct_opts if direct_mode else self.server_opts).pack(fill="x")
        self.mode_hint.configure(text=(
            "对方直接从这台电脑取件，不经过任何服务器。发送期间请保持窗口打开，取完自动结束。"
            if direct_mode else
            "先上传到常驻的收集服务，对方随时可取，这台电脑可以关机。到期或取够次数后服务端自动清理。"))
        self._update_local_hint()

    def _update_local_hint(self):
        svc = self.app.service
        if svc is not None and svc.running and svc.info:
            self.local_hint.configure(text="✓ 已自动填入本机收集服务的地址与密钥")
        else:
            self.local_hint.configure(text="")

    def set_local_service(self, url: str, key: str):
        """本窗口的收集服务启动后，自动填好「经服务器」的地址与密钥。"""
        self.url.set(url)
        if key:
            self.key.set(key)
        self._update_local_hint()

    def _load_ips(self):
        ips = direct.lan_addresses()
        self.app.bridge.call(self._set_ips, ips)

    def _set_ips(self, ips):
        self.ips = ips
        opts = [("auto", f"自动（{ips[0]}）")] + [(ip, ip) for ip in ips]
        self.addr_select.set_options(opts)

    # ---- 文件列表
    def add_paths(self, paths):
        for p in paths:
            p = os.path.abspath(p)
            if p not in self.files and os.path.exists(p):
                self.files.append(p)
                if os.path.isdir(p):
                    entries = common.collect_entries([p])
                    self._stats[p] = (len(entries), sum(os.path.getsize(f) for f, _ in entries))
                else:
                    self._stats[p] = (1, os.path.getsize(p))
        self._refresh_list()

    def add_files(self):
        self.add_paths(filedialog.askopenfilenames(parent=self.app.root, title="选择要发送的文件（可多选）"))

    def add_folder(self):
        d = filedialog.askdirectory(parent=self.app.root, title="选择要发送的文件夹")
        if d:
            self.add_paths([d])

    def remove(self, path):
        if path in self.files:
            self.files.remove(path)
        self._refresh_list()

    def clear_files(self):
        self.files.clear()
        self._refresh_list()

    def _refresh_list(self):
        for w in self.rows.winfo_children():
            w.destroy()
        if not self.files:
            self.list_card.pack_forget()
            self.drop_big.pack(fill="x", before=self.drop_btns)
            self.send_btn.set_state("disabled")
            self.compose_msg.configure(text="还没有选择文件")
            return
        self.drop_big.pack_forget()
        self.list_card.pack(fill="x", pady=(0, T.px(14)), after=self.drop_card)
        nfiles = sum(self._stats[p][0] for p in self.files)
        total = sum(self._stats[p][1] for p in self.files)
        self.count_label.configure(text=f"已选 {len(self.files)} 项 · {nfiles} 个文件 · {common.human(total)}")
        for i, p in enumerate(self.files[:80]):
            if i:
                W.hline(self.rows, pady=0)
            self._file_row(p)
        if len(self.files) > 80:
            W.label(self.rows, f"…还有 {len(self.files) - 80} 项", role="muted", bg="surface").pack(
                anchor="w", pady=T.px(6))
        self.send_btn.set_state("normal")
        self.compose_msg.configure(text="")

    def _file_row(self, p):
        r = row(self.rows, pady=T.px(6))
        is_dir = os.path.isdir(p)
        kind = file_kind(p, is_dir)
        color = T.kind_color(kind)
        W.Icon(r, "folder" if is_dir else "file", size=18, color=color,
               tile=W.mix(color, T.c["surface"], 0.86), tile_size=36).pack(side="left")
        info = W.frame(r, bg="surface")
        info.pack(side="left", fill="x", expand=True, padx=(T.px(12), T.px(8)))
        name = os.path.basename(p.rstrip("/\\")) or p
        W.label(info, W.elide(name), size=10, weight="bold").pack(anchor="w")
        n, size = self._stats[p]
        sub = f"文件夹 · {n} 个文件 · {common.human(size)}" if is_dir else common.human(size)
        W.label(info, f"{sub}  ·  {W.elide(os.path.dirname(p), 60)}", role="muted", size=9).pack(anchor="w")
        W.IconButton(r, "x", lambda: self.remove(p), size=30, tooltip="移除").pack(side="right")

    # ------------------------------------------------------------ 发送中
    def _build_share(self):
        c = W.Card(self.share, padding=24)
        c.pack(fill="x", pady=(0, T.px(14)))
        b = c.body
        top = row(b)
        self.state_pill = W.Pill(top, "准备中", "muted", dot=True)
        self.state_pill.pack(side="left")
        self.summary = W.label(top, "", role="muted")
        self.summary.pack(side="left", padx=(T.px(10), 0))

        main = row(b, pady=(T.px(16), 0))
        self.qr = W.QRCode(main, size=176)
        self.qr.pack(side="right", anchor="n", padx=(T.px(20), 0))
        left = W.frame(main, bg="surface")
        left.pack(side="left", fill="x", expand=True)
        W.label(left, "文件码", role="muted", size=9, weight="bold").pack(anchor="w")
        coderow = row(left, pady=(T.px(4), 0))
        self.code_label = W.label(coderow, "", size=17, weight="bold", mono=True, textvariable=self.code)
        self.code_label.pack(side="left")
        self.copy_code_btn = W.Button(left, "复制文件码", lambda: self._copy(self.code.get(), "文件码"),
                                      kind="primary", icon="copy")
        self.copy_code_btn.pack(anchor="w", pady=(T.px(12), T.px(16)))

        W.label(left, "装了 LAN Drop 的电脑", role="muted", size=9, weight="bold").pack(anchor="w")
        cmd = row(left, pady=(T.px(4), T.px(12)))
        W.label(cmd, "", size=9, mono=True, textvariable=self.command, role="text2").pack(side="left")
        W.IconButton(cmd, "copy", lambda: self._copy(self.command.get(), "取件命令"), size=28,
                     tooltip="复制取件命令").pack(side="left", padx=(T.px(6), 0))
        W.label(left, "手机 / 没装 LAN Drop 的设备：扫码，或用浏览器打开", role="muted", size=9,
                weight="bold").pack(anchor="w")
        web = row(left, pady=(T.px(4), 0))
        W.label(web, "", size=9, mono=True, textvariable=self.web_url, role="accent_text").pack(side="left")
        W.IconButton(web, "copy", lambda: self._copy(self.web_url.get(), "网址"), size=28,
                     tooltip="复制网址").pack(side="left", padx=(T.px(6), 0))

        self.progress = W.Progress(b, height=6)
        self.progress.pack(fill="x", pady=(T.px(20), T.px(8)))
        self.status_label = W.label(b, "", role="text2", textvariable=self.status)
        self.status_label.pack(anchor="w")
        self.alt_label = W.label(b, "", role="muted", size=9, wrap=640)
        self.alt_label.pack(anchor="w", pady=(T.px(4), 0))

        lc = W.Card(self.share, padding=18)
        lc.pack(fill="x", pady=(0, T.px(14)))
        W.label(lc.body, "动态", size=10, weight="bold").pack(anchor="w")
        self.events = W.frame(lc.body, bg="surface")
        self.events.pack(fill="x", pady=(T.px(6), 0))
        self.events_empty = W.label(lc.body, "接收端连接、每个文件送达都会显示在这里。", role="muted", size=9)

        act = row(self.share, bg="bg")
        self.cancel_btn = W.Button(act, "取消发送", self.cancel, kind="secondary", icon="x")
        self.cancel_btn.pack(side="right")
        self.again_btn = W.Button(act, "再发一批", self.reset, kind="primary", icon="plus")
        self.revoke_btn = W.Button(act, "撤销文件码", self.revoke, kind="secondary", icon="x")
        self.transfer = None        # 经服务器发送的结果：(服务地址, 密钥, 授权 ID)

    def _event(self, text):
        if self.events_empty.winfo_ismapped():
            self.events_empty.pack_forget()
        r = row(self.events, pady=T.px(3))
        W.label(r, time.strftime("%H:%M:%S"), role="muted", size=9, mono=True).pack(side="left")
        W.label(r, text, role="text2", size=9).pack(side="left", padx=(T.px(10), 0))
        kids = self.events.winfo_children()
        for w in kids[:-8]:
            w.destroy()

    def _copy(self, text, what):
        if not text:
            return
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(text)
        self.app.toast.show(f"已复制{what}")

    def _set_state(self, state, text=None):
        kinds = {"preparing": ("准备中", "muted"), "waiting": ("等待接收", "accent"),
                 "uploading": ("上传中", "accent"), "ready": ("可以取件", "accent"),
                 "done": ("已送达", "accent"), "failed": ("失败", "danger"),
                 "cancelled": ("已取消", "muted"), "timeout": ("已超时", "warn")}
        label, kind = kinds.get(state, (state, "muted"))
        self.state_pill.set(label, kind)
        if text is not None:
            self.status.set(text)
        active = state in ("preparing", "waiting", "uploading")
        self.busy = active
        self.app.set_busy("send", active)
        if active:
            self.again_btn.pack_forget()
            self.revoke_btn.pack_forget()
            self.cancel_btn.configure_text("取消发送", icon="x", kind="secondary")
            self.cancel_btn.pack(side="right")
        else:
            self.cancel_btn.pack_forget()
            self.again_btn.pack(side="right")
            if state == "ready" and self.transfer:
                self.revoke_btn.pack(side="right", padx=(0, T.px(10)))
            else:
                self.revoke_btn.pack_forget()

    def _show_share(self):
        self.compose.pack_forget()
        self.share.pack(fill="x")
        for w in self.events.winfo_children():
            w.destroy()
        self.events_empty.pack(anchor="w", pady=(T.px(4), 0))
        self.code.set("")
        self.command.set("")
        self.web_url.set("")
        self.qr.set("")
        self.alt_label.configure(text="")
        self.summary.configure(text="")
        self.progress.set(0)
        self.canvas.yview_moveto(0)

    def reset(self):
        """回到选择文件的界面（保留已选文件，方便再发一次）。"""
        if self.busy:
            return
        self.share.pack_forget()
        self.compose.pack(fill="x")
        self._refresh_list()
        self.canvas.yview_moveto(0)

    def _show_code(self, code: str):
        self.code.set(code)
        self.command.set(f"{common.cmd_prefix()} get {code}")
        url = code if code.startswith(("http://", "https://")) else "http://" + code
        self.web_url.set(url)
        self.qr.set(url)

    # ------------------------------------------------------------ 发送
    def send(self):
        if self.busy:
            return
        if not self.files:
            self.app.toast.show("请先添加文件或文件夹", "info")
            return
        self.app.save_settings()
        if self.mode.get() == "direct":
            self._send_direct()
        else:
            self._send_via_server()

    def cancel(self):
        self._cancel = True
        if self.sender is not None:
            self.sender.stop("cancelled")

    def shutdown(self):
        self.cancel()

    def _num(self, var, lo, cast=float):
        try:
            v = cast(var.get())
        except ValueError:
            raise common.CliError("请填写有效的数字") from None
        if v < lo:
            raise common.CliError(f"数值不能小于 {lo}")
        return v

    def _send_direct(self):
        try:
            timeout = self._num(self.timeout, 1)
            receivers = self._num(self.receivers, 1, int)
        except common.CliError as exc:
            self.app.toast.show(str(exc), "err")
            return
        paths = list(self.files)
        chosen = self.address.get()
        self._cancel = False
        self._show_share()
        self._set_state("preparing", "正在计算校验值…")
        ui = self.app.bridge

        def work():
            sender = None
            try:
                sender = direct.DirectSender(paths, receivers=receivers,
                                             on_event=lambda m: ui.call(self._event, m))
                n = len(sender.entries)
                done = [0]

                def hashed(name):
                    if self._cancel:
                        raise Cancelled()
                    done[0] += 1
                    ui.call(self._hash_progress, done[0], n, name)

                sender.prepare_hashes(hashed)
                if self._cancel:
                    raise Cancelled()
                sender.start()
                ips = self.ips or direct.lan_addresses()
                ip = chosen if chosen != "auto" else ips[0]
                code = common.make_code(f"http://{ip}:{sender.port}", sender.token)
                ui.call(self._direct_ready, sender, code, [i for i in ips if i != ip], n, sender.total_bytes,
                        receivers, timeout)
                reason = sender.wait(timeout * 60)
                ui.call(self._direct_finished, reason)
            except Cancelled:
                if sender is not None:
                    sender.stop("cancelled")
                ui.call(self._direct_finished, "cancelled")
            except common.CliError as exc:
                ui.call(self._fail, str(exc))
            except Exception as exc:  # noqa: BLE001
                ui.call(self._fail, f"{type(exc).__name__}: {exc}")

        run_bg(work)

    def _hash_progress(self, i, n, name):
        self.progress.set(i / max(n, 1))
        self.status.set(f"正在计算校验值 {i}/{n}：{W.elide(name, 48)}")

    def _direct_ready(self, sender, code, others, n, total, receivers, timeout):
        self.sender = sender
        self._show_code(code)
        self._set_state("waiting", "等待接收端连接…")
        who = "一次性" if receivers == 1 else f"{receivers} 人各取完一次后"
        self.summary.configure(text=f"{n} 个文件 · {common.human(total)} · {who}结束 · {timeout:g} 分钟无人取件自动结束")
        self.progress.start()
        tips = "提示：系统防火墙首次弹窗请选择「允许访问」；传输为明文 HTTP，仅限可信局域网。"
        if others:
            tips = ("对方连不上时，把文件码里的地址换成：" + "  ".join(f"{ip}:{sender.port}" for ip in others)
                    + "\n" + tips)
        self.alt_label.configure(text=tips)

    def _direct_finished(self, reason):
        self.sender = None
        self.progress.stop()
        text = {"done": "已送达，发送结束", "timeout": "超时无人取件，已停止",
                "failures": "错误尝试过多，已停止", "cancelled": "已取消"}.get(reason, reason)
        state = {"done": "done", "timeout": "timeout", "failures": "failed",
                 "cancelled": "cancelled"}.get(reason, "failed")
        self.progress.set(1 if reason == "done" else 0)
        self._set_state(state, text)
        self._event(text)
        if reason != "done":                 # 文件码已失效：别让人继续复制
            self.code.set("")
            self.qr.set("")
            self.web_url.set("")
            self.command.set("")
        else:
            self.app.notify("发送完成", "对方已取完全部文件")

    def _fail(self, msg):
        self.sender = None
        self.progress.stop()
        self.progress.set(0)
        self._set_state("failed", f"发送失败：{msg}")
        self._event(f"✗ {msg}")

    # ---- 经服务器
    def _send_via_server(self):
        try:
            expire = self._num(self.expire, 0)
            max_dl = self._num(self.max_dl, 0, int)
        except common.CliError as exc:
            self.app.toast.show(str(exc), "err")
            return
        url, key = self.url.get().strip(), self.key.get().strip()
        if not url or not key:
            self.app.toast.show("请填写服务地址和管理员密钥（本窗口启动收集服务后会自动填入）", "err")
            return
        files = list(self.files)
        self._cancel = False
        self._sent_to = (url, key)
        self.transfer = None
        self._show_share()
        self._set_state("uploading", "连接中…")
        self.qr.set("")
        ui = self.app.bridge
        started = time.time()
        totals = {"all": sum(os.path.getsize(f) for f, _ in common.collect_entries(files)), "done": 0,
                  "cur": None, "cur_done": 0}

        def progress(name, done, total):
            if self._cancel:
                raise Cancelled()
            if totals["cur"] != name:
                totals["done"] += totals["cur_done"]
                totals["cur"], totals["cur_done"] = name, 0
            totals["cur_done"] = done
            ui.call(self._upload_progress, name, totals["done"] + done, totals["all"], started)

        def work():
            try:
                res = remote.send_files(url, key, files, expire, "", "", progress, max_dl)
                ui.call(self._server_done, res)
            except Cancelled:
                ui.call(self._direct_finished, "cancelled")
            except common.CliError as exc:
                ui.call(self._fail, str(exc))
            except Exception as exc:  # noqa: BLE001
                ui.call(self._fail, f"{type(exc).__name__}: {exc}")

        run_bg(work)

    def _upload_progress(self, name, done, total, started):
        self.progress.set(done / total if total else 1)
        sp = _speed(done, started)
        self.status.set(f"正在上传 {W.elide(name, 40)}  {common.human(done)} / {common.human(total)}"
                        + (f"  ·  {sp}" if sp else ""))

    def revoke(self):
        """撤销刚才经服务器生成的文件码（等同 landrop revoke <ID>）。"""
        if not self.transfer:
            return
        if not messagebox.askokcancel("撤销文件码", "撤销后文件码立即失效，服务器上的文件会被自动清理。确定撤销？",
                                      parent=self.app.root):
            return
        url, key, gid = self.transfer
        ui = self.app.bridge

        def work():
            try:
                remote.revoke(url, key, gid)
                ui.call(self._revoked)
            except common.CliError as exc:
                ui.call(self.app.toast.show, f"撤销失败：{exc}", "err", 4000)

        run_bg(work)

    def _revoked(self):
        self.transfer = None
        self.code.set("")
        self.qr.set("")
        self.web_url.set("")
        self.command.set("")
        self._set_state("cancelled", "文件码已撤销，服务器上的文件会被自动清理。")
        self._event("已撤销")

    def _server_done(self, res):
        self.progress.set(1)
        self.transfer = (self._sent_to[0], self._sent_to[1], res["grant_id"])
        self._show_code(res["code"])
        self._set_state("ready", "已上传到服务器，对方随时可以取件；到期或取够次数后自动清理。")
        exp = f"{res['expire_hours']:g} 小时内有效" if res["expire_hours"] else "永不过期"
        times = f"可取 {res['max_downloads']} 次" if res["max_downloads"] else "取件次数不限"
        self.summary.configure(text=f"{res['count']} 个文件 · {common.human(res['bytes'])} · {exp} · {times}")
        self.alt_label.configure(text=f"随时可以点「撤销文件码」，或在管理面板 / 命令行 {common.cmd_prefix()} revoke "
                                      f"{res['grant_id']} 撤销。")
        self._event("上传完成，文件码已生成")


# =========================================================================== 接收
class ReceivePage(Page):
    title = "接收"
    subtitle = "粘贴对方给你的文件码（整条取件命令也行），文件会逐个校验 SHA-256，中断后可续传。"
    icon = "download"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        s = app.settings
        self.code = tk.StringVar()
        self.dir = tk.StringVar(value=s.get("recv_dir") or system.default_download_dir())
        self.overwrite = tk.BooleanVar(value=bool(s.get("overwrite", False)))
        self.status = tk.StringVar()
        self.busy = False
        self._cancel = False
        self.listing: list[dict] | None = None
        self.picks: dict[str, tk.BooleanVar] = {}
        self.history: list[tuple[int, str]] = []

        c = self.card(padding=22)
        self.input_card = c
        b = c.body
        W.label(b, "文件码", size=10, weight="bold").pack(anchor="w", pady=(0, T.px(6)))
        r = row(b)
        self.code_field = W.Field(r, self.code, placeholder="例如 192.168.1.5:41234/Xk3fQ…", mono=True,
                                  height=46, size=12)
        self.code_field.pack(side="left", fill="x", expand=True)
        self.code_field.entry.bind("<Return>", lambda e: self.receive())
        W.Button(r, "粘贴", self.paste, kind="secondary", icon="paste", size="lg").pack(
            side="left", padx=(T.px(10), 0))
        self.code.trace_add("write", lambda *a: self._code_changed())

        W.label(b, "保存到", size=10, weight="bold").pack(anchor="w", pady=(T.px(16), T.px(6)))
        r2 = row(b)
        W.Field(r2, self.dir).pack(side="left", fill="x", expand=True)
        W.Button(r2, "更改…", self.pick_dir, kind="secondary").pack(side="left", padx=(T.px(10), 0))
        W.IconButton(r2, "folder-open", self.open_dir, size=38, kind="secondary",
                     tooltip="打开保存目录").pack(side="left", padx=(T.px(8), 0))
        r3 = row(b, pady=(T.px(14), 0))
        W.Toggle(r3, self.overwrite, command=self.app.save_settings).pack(side="left")
        W.label(r3, "同名文件直接覆盖（默认自动改名为「名称 (1)」）", role="text2").pack(side="left", padx=(T.px(10), 0))

        act = row(b, pady=(T.px(18), 0))
        self.recv_btn = W.Button(act, "接收", self.receive, kind="primary", icon="download", size="lg")
        self.recv_btn.pack(side="right")
        self.preview_btn = W.Button(act, "先看看有哪些文件", self.preview, kind="secondary", size="lg")
        self.preview_btn.pack(side="right", padx=(0, T.px(10)))
        self.cancel_btn = W.Button(act, "取消", self.cancel, kind="secondary", icon="x", size="lg")

        # 进度 / 文件选择
        self.task_card = W.Card(self.wrap, padding=20)
        tb = self.task_card.body
        th = row(tb)
        self.task_pill = W.Pill(th, "", "muted", dot=True)
        self.task_pill.pack(side="left")
        self.task_title = W.label(th, "", size=10, weight="bold")
        self.task_title.pack(side="left", padx=(T.px(10), 0))
        self.open_btn = W.Button(th, "打开文件夹", self.open_dir, kind="soft", icon="folder-open", size="sm")
        self.toggle_all_btn = W.Button(th, "全选 / 全不选", self._toggle_all, kind="ghost", size="sm")
        self.progress = W.Progress(tb, height=6)
        self.progress.pack(fill="x", pady=(T.px(14), T.px(8)))
        self.status_label = W.label(tb, "", role="text2", textvariable=self.status, wrap=640)
        self.status_label.pack(anchor="w")
        self.file_rows = W.frame(tb, bg="surface")
        self.file_rows.pack(fill="x", pady=(T.px(10), 0))

        self.hist_card = W.Card(self.wrap, padding=18)
        W.label(self.hist_card.body, "最近接收", size=10, weight="bold").pack(anchor="w")
        self.hist_rows = W.frame(self.hist_card.body, bg="surface")
        self.hist_rows.pack(fill="x", pady=(T.px(6), 0))

    def on_show(self):
        """切到接收页时：剪贴板里像文件码就自动填上（LocalSend / AirDrop 式的省一步）。"""
        if self.code.get() or self.busy:
            return
        try:
            clip = self.app.root.clipboard_get()
        except tk.TclError:
            return
        found = common.extract_code(clip or "")
        if len(found) < 200 and CODE_RE.fullmatch(found):
            self.code.set(found)
            self.app.toast.show("已从剪贴板填入文件码", "info")

    def _code_changed(self):
        if self.listing is not None and not self.busy:
            self.listing = None
            self.task_card.pack_forget()

    def paste(self):
        try:
            self.code.set(common.extract_code(self.app.root.clipboard_get()))
        except tk.TclError:
            self.app.toast.show("剪贴板是空的", "info")

    def pick_dir(self):
        d = filedialog.askdirectory(parent=self.app.root, title="选择保存目录",
                                    initialdir=self.dir.get() or None)
        if d:
            self.dir.set(d)
            self.app.save_settings()

    def open_dir(self):
        d = os.path.expanduser(self.dir.get().strip())
        if os.path.isdir(d):
            system.open_path(d)
        else:
            self.app.toast.show("目录还不存在（第一次接收时会自动创建）", "info")

    def _inputs(self):
        code = common.extract_code(self.code.get())
        directory = os.path.expanduser(self.dir.get().strip())
        if not code:
            self.app.toast.show("请先填写文件码", "err")
            return None
        try:
            common.parse_code(code)
        except common.CliError as exc:
            self.app.toast.show(str(exc), "err")
            return None
        if not directory:
            self.app.toast.show("请选择保存目录", "err")
            return None
        if code != self.code.get():
            self.code.set(code)
        return code, directory

    def _set_busy(self, busy):
        self.busy = busy
        self.app.set_busy("receive", busy)
        for b in (self.recv_btn, self.preview_btn):
            b.set_state("disabled" if busy else "normal")
        if busy:
            self.cancel_btn.pack(side="left")
        else:
            self.cancel_btn.pack_forget()

    def _show_task(self, pill, kind, title):
        self.task_card.pack(fill="x", pady=(0, T.px(14)), after=self.input_card)
        self.task_pill.set(pill, kind)
        self.task_title.configure(text=title)
        self.open_btn.pack_forget()
        self.toggle_all_btn.pack_forget()

    # ---- 预览：列出文件，勾选后再接收（等价于 get --list / --only）
    def preview(self):
        if self.busy:
            return
        got = self._inputs()
        if not got:
            return
        code, _ = got
        self._set_busy(True)
        self._show_task("连接中", "muted", "正在读取文件列表…")
        self.progress.start()
        self.status.set("")
        for w in self.file_rows.winfo_children():
            w.destroy()
        ui = self.app.bridge

        def work():
            try:
                _cli, files = remote.list_remote(code)
                ui.call(self._show_listing, files)
            except common.CliError as exc:
                ui.call(self._failed, str(exc))
            except Exception as exc:  # noqa: BLE001
                ui.call(self._failed, f"{type(exc).__name__}: {exc}")

        run_bg(work)

    def _show_listing(self, files):
        self._set_busy(False)
        self.progress.stop()
        self.progress.set(0)
        self.listing = files
        self.picks = {}
        total = sum(f["size"] for f in files)
        self._show_task("待接收", "accent", f"共 {len(files)} 个文件 · {common.human(total)}")
        self.toggle_all_btn.pack(side="right")
        self.status.set("勾选要接收的文件，然后点「接收」。")
        for w in self.file_rows.winfo_children():
            w.destroy()
        for i, f in enumerate(files[:300]):
            if i:
                W.hline(self.file_rows)
            r = row(self.file_rows, pady=T.px(5))
            var = tk.BooleanVar(value=True)
            self.picks[f["id"]] = var
            W.Check(r, var).pack(side="left")
            kind = file_kind(f["name"])
            color = T.kind_color(kind)
            W.Icon(r, "file", size=16, color=color, tile=W.mix(color, T.c["surface"], 0.86),
                   tile_size=30, radius=8).pack(side="left", padx=(T.px(10), T.px(10)))
            W.label(r, W.elide(f["name"], 64), size=10).pack(side="left")
            W.label(r, common.human(f["size"]), role="muted", size=9).pack(side="right")
        if len(files) > 300:
            W.label(self.file_rows, f"…还有 {len(files) - 300} 个文件（默认全部接收）", role="muted",
                    bg="surface").pack(anchor="w", pady=T.px(6))

    def _toggle_all(self):
        on = not all(v.get() for v in self.picks.values())
        for v in self.picks.values():
            v.set(on)

    # ---- 接收
    def receive(self):
        if self.busy:
            return
        got = self._inputs()
        if not got:
            return
        code, directory = got
        only = None
        if self.listing is not None and self.picks:
            chosen = {fid for fid, v in self.picks.items() if v.get()}
            if not chosen:
                self.app.toast.show("至少勾选一个文件", "err")
                return
            if len(chosen) < len(self.listing):
                only = [f["name"] for f in self.listing if f["id"] in chosen]
        force = self.overwrite.get()
        self.app.save_settings()
        self._cancel = False
        self._set_busy(True)
        self._show_task("接收中", "accent", "正在连接…")
        self.progress.set(0)
        self.status.set("")
        for w in self.file_rows.winfo_children():
            w.destroy()
        ui = self.app.bridge
        started = time.time()
        acc = {"base": 0, "cur": None, "cur_done": 0, "total": 0, "n": 0}

        def prog(name, done, total):
            if self._cancel:
                raise Cancelled()
            if acc["cur"] != name:
                acc["base"] += acc["cur_done"]
                acc["cur"], acc["cur_done"] = name, 0
            acc["cur_done"] = done
            ui.call(self._recv_progress, name, acc["base"] + done, acc["total"], started)

        def one(item, target):
            acc["n"] += 1
            ui.call(self._recv_one, item, target)

        def work():
            try:
                _cli, files = remote.list_remote(code, only)
                acc["total"] = sum(f["size"] for f in files)
                ui.call(self._recv_started, len(files), acc["total"])
                saved = remote.fetch_files(code, directory, only, force, prog, one)
                ui.call(self._recv_done, len(saved), directory, acc["total"], started)
            except Cancelled:
                ui.call(self._failed, "已取消（已下载的部分会保留为 .part，重新接收时自动续传）", "已取消")
            except common.CliError as exc:
                ui.call(self._failed, str(exc))
            except Exception as exc:  # noqa: BLE001
                ui.call(self._failed, f"{type(exc).__name__}: {exc}")

        run_bg(work)

    def cancel(self):
        self._cancel = True

    def shutdown(self):
        self.cancel()

    def _recv_started(self, n, total):
        self.task_title.configure(text=f"{n} 个文件 · {common.human(total)}")

    def _recv_progress(self, name, done, total, started):
        self.progress.set(done / total if total else 1)
        sp = _speed(done, started)
        self.status.set(f"正在下载 {W.elide(name, 44)}  {common.human(done)} / {common.human(total)}"
                        + (f"  ·  {sp}" if sp else ""))

    def _recv_one(self, item, target):
        r = row(self.file_rows, pady=T.px(3))
        W.Icon(r, "check", size=14, color="accent").pack(side="left")
        W.label(r, W.elide(os.path.basename(target), 56), size=9).pack(side="left", padx=(T.px(8), 0))
        W.label(r, f"{common.human(item['size'])} · SHA-256 已校验", role="muted", size=9).pack(side="right")
        kids = self.file_rows.winfo_children()
        for w in kids[:-12]:
            w.destroy()

    def _recv_done(self, n, directory, total, started):
        self._set_busy(False)
        self.listing = None
        self.progress.set(1)
        self._show_task("完成", "accent", f"{n} 个文件 · {common.human(total)}")
        self.open_btn.pack(side="right")
        dt = time.time() - started
        self.status.set(f"完成：已保存到 {directory}" + (f"（用时 {dt:.1f} 秒）" if dt >= 1 else ""))
        self.history.insert(0, (n, directory))
        self._render_history()
        self.app.notify("接收完成", f"{n} 个文件已保存到 {directory}")

    def _failed(self, msg, pill="失败"):
        self._set_busy(False)
        self.progress.stop()
        self.progress.set(0)
        self._show_task(pill, "danger" if pill == "失败" else "muted", "没有完成")
        self.status.set(msg)

    def _render_history(self):
        for w in self.hist_rows.winfo_children():
            w.destroy()
        self.hist_card.pack(fill="x", pady=(0, T.px(14)))
        for i, (n, d) in enumerate(self.history[:5]):
            if i:
                W.hline(self.hist_rows)
            r = row(self.hist_rows, pady=T.px(4))
            W.label(r, time.strftime("%H:%M"), role="muted", size=9, mono=True).pack(side="left")
            W.label(r, f"{n} 个文件 → {W.elide(d, 60)}", size=9, role="text2").pack(side="left", padx=(T.px(10), 0))
            W.IconButton(r, "folder-open", lambda d=d: system.open_path(d) if os.path.isdir(d) else None,
                         size=28, tooltip="打开").pack(side="right")


# =========================================================================== 收集服务
class ServicePage(Page):
    title = "收集服务"
    subtitle = "把这台电脑变成局域网里的文件收集站：别人用浏览器就能上传 / 下载，不用安装任何东西。"
    icon = "server"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        s = app.settings
        from landrop.common import default_data_dir
        self.share_dir = tk.StringVar(value=os.environ.get("LANDROP_SHARE_DIR") or s.get("share_dir", ""))
        self.data_dir = tk.StringVar(value=os.environ.get("LANDROP_DATA_DIR") or s.get("data_dir")
                                     or default_data_dir())
        self.port = tk.StringVar(value=os.environ.get("LANDROP_PORT") or str(s.get("port", 8000)))
        self.scope = tk.StringVar(value=os.environ.get("LANDROP_SCOPE") or s.get("scope", "lan"))
        self.address = tk.StringVar(value=s.get("service_address", "auto"))
        self.on = tk.BooleanVar(value=False)
        self.running = False
        self.httpd = None
        self.info = None
        self.admin_key = ""

        hero = self.card(padding=22)
        hb = hero.body
        top = row(hb)
        W.Icon(top, "server", size=22, color="accent_text", tile="accent_soft", tile_size=46,
               radius=14).pack(side="left")
        tt = W.frame(top, bg="surface")
        tt.pack(side="left", padx=(T.px(14), 0))
        W.label(tt, "收集服务", size=13, weight="bold").pack(anchor="w")
        self.state_label = W.label(tt, "未启动", role="muted")
        self.state_label.pack(anchor="w")
        self.toggle = W.Toggle(top, self.on, command=self._toggled)
        self.toggle.pack(side="right")
        self.pill = W.Pill(top, "已停止", "muted", dot=True)
        self.pill.pack(side="right", padx=(0, T.px(12)))

        self.live = W.frame(hb, bg="surface")
        lv = self.live
        W.hline(lv, pady=(T.px(16), T.px(14)))
        mid = row(lv)
        self.qr = W.QRCode(mid, size=150)
        self.qr.pack(side="right", anchor="n", padx=(T.px(18), 0))
        info = W.frame(mid, bg="surface")
        info.pack(side="left", fill="x", expand=True)
        self.url_var = tk.StringVar()
        self.admin_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.dir_var = tk.StringVar()
        W.label(info, "访问地址（发给同一局域网的人）", role="muted", size=9, weight="bold").pack(anchor="w")
        u = row(info, pady=(T.px(2), T.px(10)))
        W.label(u, "", size=14, weight="bold", mono=True, textvariable=self.url_var).pack(side="left")
        W.IconButton(u, "copy", lambda: self._copy(self.url_var.get(), "访问地址"), size=30,
                     tooltip="复制").pack(side="left", padx=(T.px(6), 0))
        W.IconButton(u, "external", lambda: system.open_url(self.info["local_url"]), size=30,
                     tooltip="在浏览器中打开").pack(side="left")
        W.label(info, "管理员密钥（用来创建 / 撤销分享链接）", role="muted", size=9, weight="bold").pack(anchor="w")
        k = row(info, pady=(T.px(2), T.px(10)))
        self.key_label = W.label(k, "", size=10, mono=True, textvariable=self.key_var)
        self.key_label.pack(side="left")
        W.IconButton(k, "eye", self._toggle_key, size=30, tooltip="显示 / 隐藏").pack(side="left", padx=(T.px(6), 0))
        W.IconButton(k, "copy", lambda: self._copy(self.admin_key, "管理员密钥"), size=30,
                     tooltip="复制").pack(side="left")
        W.label(info, "共享目录", role="muted", size=9, weight="bold").pack(anchor="w")
        d = row(info, pady=(T.px(2), 0))
        W.label(d, "", size=9, textvariable=self.dir_var, role="text2").pack(side="left")
        btns = row(lv, pady=(T.px(16), 0))
        W.Button(btns, "打开管理面板", self.open_admin, kind="primary", icon="key").pack(side="left")
        W.Button(btns, "打开网页", lambda: system.open_url(self.info["local_url"]), kind="secondary",
                 icon="globe").pack(side="left", padx=(T.px(10), 0))
        W.Button(btns, "打开共享目录", self.open_folder, kind="secondary", icon="folder-open").pack(
            side="left", padx=(T.px(10), 0))
        W.Button(btns, "复制全部信息", self.copy_info, kind="ghost", icon="copy").pack(side="right")
        self.idle_hint = W.label(hb, "打开右上角的开关即可启动。首次启动会生成管理员密钥，用它在管理面板里创建分享链接；"
                                     "已上传的文件与链接都保存在数据目录里，下次启动继续可用。",
                                 role="muted", wrap=640)
        self.idle_hint.pack(anchor="w", pady=(T.px(14), 0))

        cfg = self.card(padding=20)
        cb = cfg.body
        W.label(cb, "设置", size=11, weight="bold").pack(anchor="w")
        self.cfg_hint = W.label(cb, "服务运行时不能修改，先关闭服务。", role="muted", size=9)
        self.inputs = []
        W.label(cb, "共享目录（对方上传的文件存到这里；留空 = 数据目录下按空间名分目录）", size=9,
                role="text2").pack(anchor="w", pady=(T.px(12), T.px(4)))
        r1 = row(cb)
        f1 = W.Field(r1, self.share_dir, placeholder="默认：数据目录/files/<空间名>")
        f1.pack(side="left", fill="x", expand=True)
        b1 = W.Button(r1, "选择…", lambda: self._pick(self.share_dir, "选择共享目录"), kind="secondary")
        b1.pack(side="left", padx=(T.px(10), 0))
        W.label(cb, "数据目录（链接、权限、校验记录；命令行 landrop serve 默认也用这里）", size=9,
                role="text2").pack(anchor="w", pady=(T.px(12), T.px(4)))
        r2 = row(cb)
        f2 = W.Field(r2, self.data_dir)
        f2.pack(side="left", fill="x", expand=True)
        b2 = W.Button(r2, "选择…", lambda: self._pick(self.data_dir, "选择数据目录"), kind="secondary")
        b2.pack(side="left", padx=(T.px(10), 0))
        r3 = row(cb, pady=(T.px(14), 0))
        W.label(r3, "端口", size=10, weight="bold").pack(side="left")
        f3 = W.Field(r3, self.port, width=T.px(90), height=36)
        f3.pack(side="left", padx=(T.px(10), T.px(28)))
        W.label(r3, "可访问范围", size=10, weight="bold").pack(side="left")
        self.scope_seg = W.Segmented(r3, [("lan", "局域网"), ("local", "仅本机")], self.scope)
        self.scope_seg.pack(side="left", padx=(T.px(10), 0))
        r4 = row(cb, pady=(T.px(14), 0))
        W.label(r4, "访问地址用哪个网卡", size=10, weight="bold").pack(side="left")
        W.label(r4, "多网卡 / VPN 时选同一局域网的那个", role="muted", size=9).pack(side="left", padx=(T.px(8), 0))
        self.addr_select = W.Select(r4, self.address, [("auto", "自动")], width=240)
        self.addr_select.pack(side="right")
        self.inputs = [f1, f2, f3, b1, b2]
        run_bg(lambda: self.app.bridge.call(self._set_ips, direct.lan_addresses()))

        lc = self.card(padding=18)
        lh = row(lc.body)
        W.label(lh, "服务日志", size=10, weight="bold").pack(side="left")
        self.log_btn = W.Button(lh, "展开", self._toggle_log, kind="ghost", size="sm")
        self.log_btn.pack(side="right")
        self.log = tk.Text(lc.body, height=12, wrap="word", bd=0, highlightthickness=0, padx=T.px(10),
                           pady=T.px(8), font=T.font(9, mono=True), state="disabled")
        self.log._roles = {"bg": "surface2", "fg": "text2", "insertbackground": "text"}
        self.log.configure(bg=T.c["surface2"], fg=T.c["text2"])
        self._render()

    def _set_ips(self, ips):
        self.addr_select.set_options([("auto", f"自动（{ips[0]}）")] + [(ip, ip) for ip in ips])

    # ---- 日志
    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        if int(self.log.index("end-1c").split(".")[0]) > 2000:
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _toggle_log(self):
        if self.log.winfo_ismapped():
            self.log.pack_forget()
            self.log_btn.configure_text("展开")
        else:
            self.log.pack(fill="x", pady=(T.px(8), 0))
            self.log_btn.configure_text("收起")

    def _pick(self, var, title):
        d = filedialog.askdirectory(parent=self.app.root, title=title, initialdir=var.get() or None)
        if d:
            var.set(d)

    def _copy(self, text, what):
        if text:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(text)
            self.app.toast.show(f"已复制{what}")

    def _toggle_key(self):
        shown = self.key_var.get() == self.admin_key
        self.key_var.set(self._masked() if shown else self.admin_key)

    def _masked(self):
        return "•" * min(24, max(8, len(self.admin_key))) if self.admin_key else "（见数据目录 admin-key.txt）"

    def _toggled(self):
        if self.on.get():
            self.start()
        else:
            self.stop()

    def _render(self):
        on = self.running
        self.pill.set("运行中" if on else "已停止", "accent" if on else "muted")
        if on:
            self.idle_hint.pack_forget()
            self.live.pack(fill="x")
            scope = "局域网内的设备都能访问" if self.info["host"] == "0.0.0.0" else "仅这台电脑可以访问"
            self.state_label.configure(text=f"端口 {self.info['port']} · {scope}")
            self.cfg_hint.pack(anchor="w", pady=(T.px(4), 0))
        else:
            self.live.pack_forget()
            self.idle_hint.pack(anchor="w", pady=(T.px(14), 0))
            self.state_label.configure(text="未启动")
            self.cfg_hint.pack_forget()
        for w in self.inputs:
            (w.set_enabled if hasattr(w, "set_enabled") else
             (lambda st, w=w: w.set_state("normal" if st else "disabled")))(not on)
        self.scope_seg.set_enabled(not on)
        self.addr_select.configure(cursor="arrow" if on else "hand2")
        self.addr_select.enabled = not on
        if self.on.get() != on:
            self.on.set(on)
        self.app.set_service_state(on)

    # ---- 启停
    def start(self):
        from landrop.server import app as srv
        if self.running:
            return
        data_dir = self.data_dir.get().strip()
        share_dir = self.share_dir.get().strip()
        try:
            port = int(self.port.get().strip() or "8000")
            assert 0 < port < 65536
        except (ValueError, AssertionError):
            self.on.set(False)
            self.app.toast.show("端口必须是 1–65535 之间的数字", "err")
            return
        host = "127.0.0.1" if self.scope.get() == "local" else "0.0.0.0"
        try:
            if share_dir:
                os.makedirs(share_dir, exist_ok=True)
            addr = self.address.get()
            public = f"http://{addr}:{port}" if (addr != "auto" and host == "0.0.0.0") else ""
            self.info = srv.prepare(data_dir=data_dir or common.default_data_dir(),
                                    share_dir=share_dir, host=host, port=port, public_url=public)
            self.httpd = srv.make_server(host, port)
        except OSError as exc:
            self.on.set(False)
            busy = getattr(exc, "errno", None) in (98, 48, 10048)
            messagebox.showerror("启动失败", f"端口 {port} 已被占用，换一个端口再试。" if busy else str(exc),
                                 parent=self.app.root)
            return
        except Exception as exc:  # noqa: BLE001
            self.on.set(False)
            messagebox.showerror("启动失败", f"{exc}\n\n{traceback.format_exc(limit=3)}", parent=self.app.root)
            return
        run_bg(self._serve)
        self.running = True
        info = self.info
        key = info["admin_key"]
        if not key:
            try:
                with open(info["admin_key_file"], encoding="utf-8") as f:
                    key = f.read().strip()
            except OSError:
                key = ""
        self.admin_key = key
        self.url_var.set(info["url"])
        self.key_var.set(self._masked())
        self.dir_var.set(W.elide(info["share_dir"], 70))
        if host == "0.0.0.0":
            self.qr.set(info["url"])
            self.qr.pack(side="right", anchor="n", padx=(T.px(18), 0))
        else:                           # 仅本机：二维码扫了也打不开
            self.qr.pack_forget()
        self.app.send.set_local_service(info["local_url"], key)
        self.append_log(f"\n—— 已启动 {time.strftime('%H:%M:%S')}  {info['url']}  数据目录 {info['data_dir']}\n")
        self.app.save_settings()
        self._render()
        if info["admin_key"]:
            self.app.toast.show("服务已启动，已生成新的管理员密钥", "ok", 3000)
        else:
            self.app.toast.show("服务已启动")

    def _serve(self):
        try:
            self.httpd.serve_forever()
        except Exception as exc:  # noqa: BLE001
            msg = f"\n服务异常：{exc!r}\n"
            self.app.bridge.call(self.append_log, msg)

    def stop(self):
        if self.httpd:
            httpd, self.httpd = self.httpd, None
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        was = self.running
        self.running = False
        self._render()
        self.app.send._update_local_hint()
        if was:
            self.append_log(f"—— 已停止 {time.strftime('%H:%M:%S')}（文件与链接都保留在数据目录中）\n")

    def open_admin(self):
        if self.info:
            url = self.info["local_url"] + "/admin"
            if self.admin_key:      # 片段里的密钥不会发给服务器，网页读完立刻从地址栏抹掉
                url += "#k=" + quote(self.admin_key, safe="")
            system.open_url(url)

    def open_folder(self):
        if self.info:
            try:
                system.open_path(self.info["share_dir"])
            except Exception as exc:  # noqa: BLE001
                self.app.toast.show(f"无法打开：{exc}", "err")

    def copy_info(self):
        if not self.info:
            return
        text = (f"访问地址：{self.info['url']}\n管理面板：{self.info['local_url']}/admin\n"
                f"管理员密钥：{self.admin_key or '见 ' + self.info['admin_key_file']}\n")
        self._copy(text, "访问信息")


# =========================================================================== 设置
class SettingsPage(Page):
    title = "设置"
    subtitle = "外观、命令行工具与数据位置。"
    icon = "settings"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.theme_var = tk.StringVar(value=T.pref)

        c = self.card(padding=20)
        r = row(c.body)
        left = W.frame(r, bg="surface")
        left.pack(side="left")
        W.label(left, "外观", size=11, weight="bold").pack(anchor="w")
        W.label(left, "跟随系统的浅色 / 深色设置，或固定一种", role="muted", size=9).pack(anchor="w")
        W.Segmented(r, [("system", "跟随系统"), ("light", "浅色"), ("dark", "深色")], self.theme_var,
                    command=lambda: app.apply_theme(self.theme_var.get())).pack(side="right")

        cli = self.card(padding=20)
        cb = cli.body
        top = row(cb)
        W.Icon(top, "terminal", size=20, color="text2", tile="surface3", tile_size=40, radius=12).pack(side="left")
        tt = W.frame(top, bg="surface")
        tt.pack(side="left", padx=(T.px(12), 0))
        W.label(tt, "命令行工具 landrop", size=11, weight="bold").pack(anchor="w")
        self.cli_state = W.label(tt, "", role="muted", size=9)
        self.cli_state.pack(anchor="w")
        self.cli_btn = W.Button(top, "添加到 PATH", self.toggle_cli, kind="primary")
        self.cli_btn.pack(side="right")
        self.cli_pill = W.Pill(top, "", "muted", dot=True)
        self.cli_pill.pack(side="right", padx=(0, T.px(10)))
        self.cli_note = W.label(cb, "", role="muted", size=9, wrap=640)
        self.cli_note.pack(anchor="w", pady=(T.px(10), 0))
        ex = W.Card(cb, padding=14, fill="surface2", border=None, radius=12)
        ex.pack(fill="x", pady=(T.px(12), 0))
        for cmd, what in (("landrop send 文件或文件夹", "直连发送，输出文件码（加 --qr 显示二维码）"),
                          ("landrop get <文件码> -o 目录", "接收；--list 只看列表，--only 只取部分"),
                          ("landrop send 文件 --server URL --key 密钥", "上传到收集服务，生成一次性取件码"),
                          ("landrop serve", "在终端里启动收集服务（与本窗口用同一个数据目录）")):
            rr = row(ex.body, bg="surface2", pady=T.px(3))
            W.label(rr, cmd, size=9, mono=True, bg="surface2").pack(side="left")
            W.label(rr, what, role="muted", size=9, bg="surface2").pack(side="right")

        dc = self.card(padding=20)
        db = dc.body
        hr = row(db)
        W.label(hr, "数据", size=11, weight="bold").pack(side="left")
        kind = system.install_kind()
        if kind == "portable":
            W.Pill(hr, "免安装版：数据跟着程序目录走", "accent").pack(side="left", padx=(T.px(10), 0))
        from landrop.common import default_data_dir
        dd = os.environ.get("LANDROP_DATA_DIR") or default_data_dir()
        r2 = row(db, pady=(T.px(10), 0))
        W.label(r2, W.elide(dd, 72), role="text2", size=9, mono=True).pack(side="left")
        W.Button(r2, "打开", lambda: system.open_path(dd) if os.path.isdir(dd) else
                 self.app.toast.show("目录还不存在（第一次启动服务时创建）", "info"),
                 kind="secondary", size="sm", icon="folder-open").pack(side="right")
        W.label(db, "收集服务的状态库、文件、管理员密钥（admin-key.txt）与窗口设置都在这里。",
                role="muted", size=9).pack(anchor="w", pady=(T.px(6), 0))

        ac = self.card(padding=20)
        ab = ac.body
        r3 = row(ab)
        self.logo = tk.Label(r3, image=app.logo(48), bd=0, bg=T.c["surface"])
        self.logo._roles = {"bg": "surface"}
        self.logo.pack(side="left")
        info = W.frame(r3, bg="surface")
        info.pack(side="left", padx=(T.px(14), 0))
        W.label(info, f"LAN Drop {__version__}", size=12, weight="bold").pack(anchor="w")
        kinds = {"portable": "免安装版", "onefile": "免安装版（单文件）", "appimage": "免安装版（AppImage）",
                 "installed": "安装版", "source": "源码运行"}
        W.label(info, f"{kinds[kind]} · 局域网文件互传与收集 · MIT 协议", role="muted", size=9).pack(anchor="w")
        W.Button(r3, "项目主页", lambda: system.open_url("https://github.com/Introl-ljl/landrop"),
                 kind="secondary", icon="external", size="sm").pack(side="right")

    def on_show(self):
        self.refresh_cli()

    def refresh_cli(self):
        st = system.cli_status()
        self._cli = st
        if not st["available"]:
            self.cli_pill.set("单文件版不含" if system.is_onefile() else "源码运行", "muted")
            self.cli_state.configure(text=st["location"] or "python -m landrop")
            self.cli_btn.pack_forget()
        else:
            self.cli_pill.set("已可用" if st["installed"] else "未添加", "accent" if st["installed"] else "muted")
            self.cli_state.configure(text=st["location"] or "安装包已自带，添加到 PATH 后终端里可直接使用")
            if st["can_toggle"]:
                self.cli_btn.configure_text("从 PATH 移除" if st["installed"] else "添加到 PATH",
                                            kind="secondary" if st["installed"] else "primary")
                self.cli_btn.pack(side="right")
            else:
                self.cli_btn.pack_forget()
        self.cli_note.configure(text=st["note"])

    def toggle_cli(self):
        try:
            msg = system.uninstall_cli() if self._cli["installed"] else system.install_cli()
            self.app.toast.show(msg, "ok", 3200)
        except Exception as exc:  # noqa: BLE001
            self.app.toast.show(f"操作失败：{exc}", "err", 4000)
        self.refresh_cli()
