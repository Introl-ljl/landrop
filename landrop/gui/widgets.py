# -*- coding: utf-8 -*-
"""自绘控件：圆角卡片、按钮、分段控件、开关、进度条、输入框、图标……只依赖 tkinter。

Tk 的 Canvas 在 Windows / Linux 上画圆角与斜线没有抗锯齿，这正是「Win7 味」的来源之一。
这里的做法：圆角与图标都先在内存里超采样光栅化（4×4），按已知底色混合成小图（PhotoImage），
再贴到 Canvas 上；直边用矩形补齐。图按 (形状, 尺寸, 颜色) 缓存，换主题 / 悬停只是换图。
"""
from __future__ import annotations

import math
import tkinter as tk

from .theme import T

# =========================================================================== 光栅化
_IMG_CACHE: dict = {}


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def mix(a: str, b: str, t: float) -> str:
    """a→b 线性混合，t=0 为 a。"""
    ra, ga, ba = _hex(a)
    rb, gb, bb = _hex(b)
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t),
                              round(ba + (bb - ba) * t))


def _photo(width: int, height: int, pixels: list[str]) -> tk.PhotoImage:
    img = tk.PhotoImage(width=width, height=height)
    rows = []
    for y in range(height):
        rows.append("{" + " ".join(pixels[y * width:(y + 1) * width]) + "}")
    img.put(" ".join(rows))
    return img


def _corner(r: int, fill: str, outline: str | None, bw: int, bg: str, which: str) -> tk.PhotoImage:
    """r×r 的圆角（which: nw/ne/sw/se），内部 fill、宽 bw 的描边 outline、外部 bg。"""
    key = ("corner", r, fill, outline, bw, bg, which)
    img = _IMG_CACHE.get(key)
    if img is not None:
        return img
    n = 4
    pixels = []
    inner = r - bw if outline else r
    for y in range(r):
        for x in range(r):
            c_out = c_line = c_in = 0
            for sy in range(n):
                for sx in range(n):
                    px = x + (sx + 0.5) / n
                    py = y + (sy + 0.5) / n
                    # 以圆心 (r, r) 计算到「左上角」形状的距离，再按 which 翻转
                    dx = r - px if which in ("nw", "sw") else px
                    dy = r - py if which in ("nw", "ne") else py
                    d = math.hypot(dx, dy) if (dx > 0 and dy > 0) else max(dx, dy)
                    if d > r:
                        c_out += 1
                    elif d > inner:
                        c_line += 1
                    else:
                        c_in += 1
            tot = n * n
            rr = gg = bb = 0.0
            for col, cnt in ((bg, c_out), (outline or fill, c_line), (fill, c_in)):
                if cnt:
                    cr, cg, cb = _hex(col)
                    rr += cr * cnt
                    gg += cg * cnt
                    bb += cb * cnt
            pixels.append("#%02x%02x%02x" % (round(rr / tot), round(gg / tot), round(bb / tot)))
    img = _photo(r, r, pixels)
    _IMG_CACHE[key] = img
    return img


def rounded(cv: tk.Canvas, x0, y0, x1, y1, r, fill, outline=None, bw=1, bg=None, tags=()):
    """在 Canvas 上画抗锯齿圆角矩形。bg 是它背后的颜色（圆角外侧用它混合）。"""
    x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    r = int(max(0, min(r, w // 2, h // 2)))
    bg = bg or cv.cget("bg")
    bw = max(1, int(bw)) if outline else 0
    edge = outline or fill
    if r == 0:
        cv.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline or "", width=bw, tags=tags)
        return
    # 中间的填充
    cv.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, width=0, outline="", tags=tags)
    cv.create_rectangle(x0, y0 + r, x0 + r, y1 - r, fill=fill, width=0, outline="", tags=tags)
    cv.create_rectangle(x1 - r, y0 + r, x1, y1 - r, fill=fill, width=0, outline="", tags=tags)
    if outline:          # 四条直边描边
        cv.create_rectangle(x0 + r, y0, x1 - r, y0 + bw, fill=edge, width=0, outline="", tags=tags)
        cv.create_rectangle(x0 + r, y1 - bw, x1 - r, y1, fill=edge, width=0, outline="", tags=tags)
        cv.create_rectangle(x0, y0 + r, x0 + bw, y1 - r, fill=edge, width=0, outline="", tags=tags)
        cv.create_rectangle(x1 - bw, y0 + r, x1, y1 - r, fill=edge, width=0, outline="", tags=tags)
    for which, (cx, cy) in (("nw", (x0, y0)), ("ne", (x1 - r, y0)),
                            ("sw", (x0, y1 - r)), ("se", (x1 - r, y1 - r))):
        cv.create_image(cx, cy, image=_corner(r, fill, outline, bw, bg, which), anchor="nw", tags=tags)


# --------------------------------------------------------------------------- 图标
def _rounded_path(pts, r, closed=False, steps=6):
    """折线拐角按半径 r 倒圆，返回新的点列。"""
    if r <= 0 or len(pts) < 3:
        return list(pts) + ([pts[0]] if closed else [])
    out = []
    n = len(pts)
    rng = range(n) if closed else range(1, n - 1)
    if not closed:
        out.append(pts[0])
    for i in rng:
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]
        v1 = (p0[0] - p1[0], p0[1] - p1[1])
        v2 = (p2[0] - p1[0], p2[1] - p1[1])
        l1, l2 = math.hypot(*v1), math.hypot(*v2)
        if l1 == 0 or l2 == 0:
            out.append(p1)
            continue
        rr = min(r, l1 / 2, l2 / 2)
        a = (p1[0] + v1[0] / l1 * rr, p1[1] + v1[1] / l1 * rr)
        b = (p1[0] + v2[0] / l2 * rr, p1[1] + v2[1] / l2 * rr)
        for k in range(steps + 1):      # 二次贝塞尔近似圆角
            t = k / steps
            out.append(((1 - t) ** 2 * a[0] + 2 * (1 - t) * t * p1[0] + t * t * b[0],
                        (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * p1[1] + t * t * b[1]))
    if closed:
        out.append(out[0])
    else:
        out.append(pts[-1])
    return out


def _circle(cx, cy, r, a0=0, a1=360, steps=40):
    n = max(6, int(steps * abs(a1 - a0) / 360))
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def _rrect(x0, y0, x1, y1, r):
    return _rounded_path([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], r, closed=True)


# 24×24 网格、线宽 2 的线性图标（风格参照 Lucide / Feather，形状自绘）
ICONS = {
    "send": [[(22, 2), (11, 13)], _rounded_path([(22, 2), (15, 22), (11, 13), (2, 9)], 1, True)],
    "download": [_rounded_path([(3, 15), (3, 21), (21, 21), (21, 15)], 2),
                 [(7, 10), (12, 15), (17, 10)], [(12, 15), (12, 3)]],
    "upload": [_rounded_path([(3, 15), (3, 21), (21, 21), (21, 15)], 2),
               [(17, 8), (12, 3), (7, 8)], [(12, 3), (12, 15)]],
    "server": [_rrect(2, 2, 22, 10, 2), _rrect(2, 14, 22, 22, 2), ("dot", 6, 6, 1.3), ("dot", 6, 18, 1.3)],
    "settings": [[(21, 4), (14, 4)], [(10, 4), (3, 4)], [(21, 12), (12, 12)], [(8, 12), (3, 12)],
                 [(21, 20), (16, 20)], [(12, 20), (3, 20)], [(14, 2), (14, 6)], [(8, 10), (8, 14)],
                 [(16, 18), (16, 22)]],
    "file": [_rounded_path([(14, 2), (5, 2), (5, 22), (19, 22), (19, 7), (14, 2)], 2, True),
             [(14, 2), (14, 7), (19, 7)]],
    "folder": [_rounded_path([(2, 4), (9, 4), (11, 7), (22, 7), (22, 20), (2, 20)], 2, True)],
    "plus": [[(12, 5), (12, 19)], [(5, 12), (19, 12)]],
    "x": [[(18, 6), (6, 18)], [(6, 6), (18, 18)]],
    "copy": [_rrect(8, 8, 22, 22, 2), _rounded_path([(4, 16), (2, 16), (2, 2), (16, 2), (16, 4)], 2)],
    "check": [[(20, 6), (9, 17), (4, 12)]],
    "check-circle": [_circle(12, 12, 10), [(8, 12), (11, 15), (16, 9)]],
    "paste": [_rrect(8, 2, 16, 6, 1), _rounded_path([(16, 4), (20, 4), (20, 22), (4, 22), (4, 4), (8, 4)], 2)],
    "external": [[(15, 3), (21, 3), (21, 9)], [(10, 14), (21, 3)],
                 _rounded_path([(18, 13), (18, 21), (3, 21), (3, 6), (11, 6)], 2)],
    "folder-open": [_rounded_path([(2, 19), (2, 4), (9, 4), (11, 7), (19, 7), (19, 10)], 2),
                    _rounded_path([(2, 19), (6, 10), (22, 10), (18, 19), (2, 19)], 1.5)],
    "globe": [_circle(12, 12, 10), [(2, 12), (22, 12)],
              [(12 + 4 * math.sin(math.radians(a)), 12 - 10 * math.cos(math.radians(a)))
               for a in range(0, 181, 12)],
              [(12 - 4 * math.sin(math.radians(a)), 12 - 10 * math.cos(math.radians(a)))
               for a in range(0, 181, 12)]],
    "key": [_circle(7.5, 15.5, 5.5), [(21, 2), (11.4, 11.6)], [(15.5, 7.5), (18.5, 10.5), (22, 7), (19, 4)]],
    "terminal": [[(4, 17), (10, 11), (4, 5)], [(12, 19), (20, 19)]],
    "info": [_circle(12, 12, 10), [(12, 16), (12, 12)], ("dot", 12, 8, 1.3)],
    "alert": [_rounded_path([(12, 3), (22, 20), (2, 20)], 2, True), [(12, 9), (12, 13)], ("dot", 12, 16.5, 1.3)],
    "arrow-right": [[(5, 12), (19, 12)], [(12, 5), (19, 12), (12, 19)]],
    "trash": [[(3, 6), (21, 6)], _rounded_path([(19, 6), (19, 21), (5, 21), (5, 6)], 2),
              _rounded_path([(8, 6), (8, 3), (16, 3), (16, 6)], 1.5)],
    "phone": [_rrect(6, 2, 18, 22, 2.5), ("dot", 12, 18, 1.2)],
    "eye": [[(2 + 20 * i / 24, 12 - 7 * math.sin(math.pi * i / 24) ** 0.9) for i in range(25)],
            [(2 + 20 * i / 24, 12 + 7 * math.sin(math.pi * i / 24) ** 0.9) for i in range(25)],
            _circle(12, 12, 3)],
    "chevron-down": [[(6, 9), (12, 15), (18, 9)]],
    "minus": [[(5, 12), (19, 12)]],
    "power": [[(12, 2), (12, 12)], _circle(12, 13, 8.5, -60, 240)],
    "stop": [_rrect(6, 6, 18, 18, 2)],
    "qr": [_rrect(3, 3, 10, 10, 1), _rrect(14, 3, 21, 10, 1), _rrect(3, 14, 10, 21, 1),
           [(14, 14), (14, 17)], [(17, 14), (21, 14)], [(21, 17), (21, 21), (17, 21)], [(14, 21), (14, 21.01)]],
}

_COV_CACHE: dict = {}


def _coverage(name: str, size: int, stroke: float) -> list[float]:
    key = (name, size, stroke)
    cov = _COV_CACHE.get(key)
    if cov is not None:
        return cov
    n = 4
    s = size * n
    k = s / 24.0
    mask = bytearray(s * s)
    hw = stroke * k / 2
    for prim in ICONS[name]:
        if isinstance(prim, tuple) and prim and prim[0] == "dot":
            _, cx, cy, r = prim
            cx, cy, r = cx * k, cy * k, r * k
            for yy in range(max(0, int(cy - r)), min(s, int(cy + r) + 2)):
                for xx in range(max(0, int(cx - r)), min(s, int(cx + r) + 2)):
                    if (xx + 0.5 - cx) ** 2 + (yy + 0.5 - cy) ** 2 <= r * r:
                        mask[yy * s + xx] = 1
            continue
        pts = [(x * k, y * k) for x, y in prim]
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            dx, dy = bx - ax, by - ay
            ll = dx * dx + dy * dy
            for yy in range(max(0, int(min(ay, by) - hw)), min(s, int(max(ay, by) + hw) + 2)):
                py = yy + 0.5
                for xx in range(max(0, int(min(ax, bx) - hw)), min(s, int(max(ax, bx) + hw) + 2)):
                    px = xx + 0.5
                    t = 0.0 if ll == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
                    qx, qy = ax + t * dx - px, ay + t * dy - py
                    if qx * qx + qy * qy <= hw * hw:
                        mask[yy * s + xx] = 1
    cov = []
    for y in range(size):
        for x in range(size):
            c = 0
            for sy in range(n):
                row = (y * n + sy) * s + x * n
                c += sum(mask[row:row + n])
            cov.append(c / (n * n))
    _COV_CACHE[key] = cov
    return cov


def icon_image(name: str, size: int, fg: str, bg: str, stroke: float = 2.0) -> tk.PhotoImage:
    key = ("icon", name, size, fg, bg, stroke)
    img = _IMG_CACHE.get(key)
    if img is None:
        fr, fg_, fb = _hex(fg)
        br, bgg, bb = _hex(bg)
        pixels = ["#%02x%02x%02x" % (round(br + (fr - br) * a), round(bgg + (fg_ - bgg) * a),
                                     round(bb + (fb - bb) * a)) for a in _coverage(name, size, stroke)]
        img = _photo(size, size, pixels)
        _IMG_CACHE[key] = img
    return img


# =========================================================================== 基础
def bg_of(widget) -> str:
    try:
        return widget.cget("bg")
    except tk.TclError:
        return T.c["bg"]


class Themed:
    """自绘控件的基类：换主题时 recolor() 会被调用。"""

    def recolor(self):
        pass


def recolor_tree(widget):
    """换主题：自上而下重新着色（子控件读取父控件的新底色，所以父先子后）。"""
    roles = getattr(widget, "_roles", None)
    if roles:
        opts = {k: T.c[v] for k, v in roles.items() if v}
        try:
            widget.configure(**opts)
        except tk.TclError:
            pass
    if isinstance(widget, Themed):
        widget.recolor()
    for child in widget.winfo_children():
        recolor_tree(child)


def frame(parent, bg="bg", **kw) -> tk.Frame:
    f = tk.Frame(parent, bg=T.c[bg], highlightthickness=0, bd=0, **kw)
    f._roles = {"bg": bg}
    return f


def label(parent, text="", role="text", size=10, weight="normal", bg=None, mono=False,
          wrap=None, justify="left", anchor="w", textvariable=None, **kw) -> tk.Label:
    bgrole = bg or getattr(parent, "_roles", {}).get("bg", "bg")
    lab = tk.Label(parent, text=text, textvariable=textvariable, fg=T.c[role], bg=T.c[bgrole],
                   font=T.font(size, weight, mono), justify=justify, anchor=anchor,
                   bd=0, highlightthickness=0, padx=0, pady=0, **kw)
    if wrap:
        lab.configure(wraplength=T.px(wrap))
    lab._roles = {"fg": role, "bg": bgrole}
    return lab


class Canvas(tk.Canvas, Themed):
    def __init__(self, parent, **kw):
        bgc = kw.pop("bg", None) or bg_of(parent)
        super().__init__(parent, bg=bgc, highlightthickness=0, bd=0, **kw)

    def sync_bg(self):
        self.configure(bg=bg_of(self.master))


# =========================================================================== 卡片
class Card(Canvas):
    """圆角卡片：card.body 是放内容的 Frame，高度随内容自动变化。"""

    def __init__(self, parent, padding=18, radius=16, fill="surface", border="line", **kw):
        super().__init__(parent, **kw)
        self.fill_role, self.border_role = fill, border
        self.pad, self.radius = T.px(padding), T.px(radius)
        self.body = frame(self, bg=fill)
        self._win = self.create_window(self.pad, self.pad, window=self.body, anchor="nw")
        self.bind("<Configure>", self._redraw)
        self.body.bind("<Configure>", self._fit)

    def _fit(self, _e=None):
        h = self.body.winfo_reqheight() + 2 * self.pad
        if int(self.cget("height")) != h:
            self.configure(height=h)

    def _redraw(self, _e=None):
        w, h = self.winfo_width(), self.winfo_height()
        self.itemconfigure(self._win, width=max(1, w - 2 * self.pad))
        self.delete("bgshape")
        rounded(self, 0, 0, w, h, self.radius, T.c[self.fill_role],
                T.c[self.border_role] if self.border_role else None, max(1, T.px(1)),
                bg_of(self.master), tags=("bgshape",))
        self.tag_lower("bgshape")

    def recolor(self):
        self.sync_bg()
        self._redraw()


# =========================================================================== 按钮
class Button(Canvas):
    """kind: primary（主色）/ secondary（浅底）/ ghost（透明）/ danger / soft（主色浅底）"""

    SIZES = {"sm": (30, 10, 9, 15), "md": (38, 14, 10, 17), "lg": (46, 20, 11, 19)}

    def __init__(self, parent, text="", command=None, kind="secondary", icon=None, size="md",
                 width=None, radius=None, **kw):
        h, padx, fsize, isize = self.SIZES[size]
        self._h, self._padx, self._fsize, self._isize = T.px(h), T.px(padx), fsize, T.px(isize)
        self._radius = T.px(radius if radius is not None else (10 if size != "lg" else 12))
        self.text, self.icon, self.kind, self.command = text, icon, kind, command
        self._fixed_w = T.px(width) if width else None
        self.state = "normal"
        self._hover = self._press = self._focus = False
        super().__init__(parent, height=self._h, cursor="hand2", takefocus=1, **kw)
        self._measure()
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, press=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(press=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda e: self._set(focus=True))
        self.bind("<FocusOut>", lambda e: self._set(focus=False))
        self.bind("<Key-space>", lambda e: self.invoke())
        self.bind("<Return>", lambda e: self.invoke())
        self.bind("<Configure>", lambda e: self.draw())

    def _measure(self):
        import tkinter.font as tkfont
        f = tkfont.Font(font=T.font(self._fsize, "bold"))
        w = f.measure(self.text) if self.text else 0
        if self.icon:
            w += self._isize + (T.px(7) if self.text else 0)
        self.configure(width=self._fixed_w or (w + 2 * self._padx if self.text else self._h))

    def configure_text(self, text=None, icon=None, kind=None):
        if text is not None:
            self.text = text
        if icon is not None:
            self.icon = icon or None
        if kind is not None:
            self.kind = kind
        self._measure()
        self.draw()

    def set_state(self, state: str):
        self.state = state
        self.configure(cursor="hand2" if state == "normal" else "arrow",
                       takefocus=1 if state == "normal" else 0)
        self.draw()

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, "_" + k, v)
        self.draw()

    def _release(self, e):
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        was = self._press
        self._set(press=False)
        if was and inside:
            self.invoke()

    def invoke(self):
        if self.state == "normal" and self.command:
            self.command()

    def colors(self):
        c, k = T.c, self.kind
        disabled = self.state != "normal"
        if k == "primary":
            fill = c["accent_press"] if self._press else c["accent_hover"] if self._hover else c["accent"]
            fg, line = c["accent_ink"], None
            if disabled:
                fill, fg = mix(c["accent"], c["bg"], 0.55), mix(c["accent_ink"], c["bg"], 0.45)
        elif k == "danger":
            fill = c["danger"] if not (self._hover or self._press) else mix(c["danger"], "#000000", 0.12)
            fg, line = ("#ffffff" if T.mode == "light" else "#1a0507"), None
            if disabled:
                fill = mix(c["danger"], c["bg"], 0.55)
        elif k == "soft":
            fill = c["accent_soft_hover"] if (self._hover or self._press) else c["accent_soft"]
            fg, line = c["accent_text"], None
            if disabled:
                fg = c["muted"]
        elif k == "ghost":
            base = bg_of(self.master)
            fill = c["press"] if self._press else c["hover"] if self._hover else base
            fg, line = c["text2"], None
            if disabled:
                fg = c["line2"]
        else:   # secondary
            base = c["surface"]
            fill = c["press"] if self._press else c["hover"] if self._hover else base
            fg, line = c["text"], c["line2"]
            if disabled:
                fg = c["muted"]
                fill = c["surface2"]
        return fill, fg, line

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self._h
        if w <= 1:
            w = int(self.cget("width"))
        bg = bg_of(self.master)
        self.configure(bg=bg)
        fill, fg, line = self.colors()
        rounded(self, 0, 0, w, h, self._radius, fill, line, max(1, T.px(1)), bg)
        if self._focus and self.state == "normal":
            rounded(self, 0, 0, w, h, self._radius, fill, T.c["focus"], max(2, T.px(2)), bg)
        import tkinter.font as tkfont
        font = T.font(self._fsize, "bold")
        tw = tkfont.Font(font=font).measure(self.text) if self.text else 0
        iw = self._isize if self.icon else 0
        gap = T.px(7) if (self.icon and self.text) else 0
        x = (w - tw - iw - gap) // 2
        if self.icon:
            self.create_image(x, h // 2, image=icon_image(self.icon, self._isize, fg, fill), anchor="w")
            x += iw + gap
        if self.text:
            self.create_text(x, h // 2, text=self.text, fill=fg, font=font, anchor="w")

    def recolor(self):
        self.draw()


class IconButton(Button):
    """只有图标的方形按钮（默认透明底）。"""

    def __init__(self, parent, icon, command=None, size=32, kind="ghost", tooltip=None, **kw):
        super().__init__(parent, text="", command=command, kind=kind, icon=icon, size="sm",
                         width=size, radius=8, **kw)
        self._h = T.px(size)
        self._isize = T.px(int(size * 0.5))
        self.configure(height=self._h, width=self._h)
        if tooltip:
            Tooltip(self, tooltip)


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text, self.tip, self._job = widget, text, None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._job = self.widget.after(450, self._show)

    def _show(self):
        if self.tip:
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + T.px(6)
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        lab = tk.Label(self.tip, text=self.text, bg=T.c["text"], fg=T.c["bg"],
                       font=T.font(9), padx=T.px(8), pady=T.px(4))
        lab.pack()
        self.tip.update_idletasks()
        self.tip.wm_geometry(f"+{x - self.tip.winfo_width() // 2}+{y}")

    def _hide(self, _e=None):
        if self._job:
            self.widget.after_cancel(self._job)
            self._job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


# =========================================================================== 分段控件
class Segmented(Canvas):
    def __init__(self, parent, options, variable: tk.StringVar, command=None, size=10, **kw):
        self.options, self.var, self.command = options, variable, command
        self._fsize = size
        self._h = T.px(36)
        super().__init__(parent, height=self._h, cursor="hand2", **kw)
        import tkinter.font as tkfont
        f = tkfont.Font(font=T.font(size, "bold"))
        self._widths = [f.measure(lab) + T.px(32) for _v, lab in options]
        self.configure(width=sum(self._widths) + T.px(8))
        self._hover = None
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._sethover(None))
        self.bind("<Configure>", lambda e: self.draw())
        variable.trace_add("write", lambda *a: self.draw())

    def _index_at(self, x):
        pos = T.px(4)
        for i, w in enumerate(self._widths):
            if pos <= x < pos + w:
                return i
            pos += w
        return None

    def _motion(self, e):
        self._sethover(self._index_at(e.x))

    def _sethover(self, i):
        if i != self._hover:
            self._hover = i
            self.draw()

    def _click(self, e):
        i = self._index_at(e.x)
        if i is not None and self.state_ok():
            self.var.set(self.options[i][0])
            if self.command:
                self.command()

    def state_ok(self):
        return str(self.cget("cursor")) == "hand2"

    def set_enabled(self, on: bool):
        self.configure(cursor="hand2" if on else "arrow")
        self.draw()

    def draw(self):
        self.delete("all")
        c = T.c
        bg = bg_of(self.master)
        self.configure(bg=bg)
        w, h = int(self.cget("width")), self._h
        rounded(self, 0, 0, w, h, T.px(10), c["surface3"], None, 1, bg)
        pos = T.px(4)
        on = self.state_ok()
        for i, (val, lab) in enumerate(self.options):
            sw = self._widths[i]
            sel = self.var.get() == val
            if sel:
                rounded(self, pos, T.px(4), pos + sw, h - T.px(4), T.px(7), c["surface"],
                        c["line"], 1, c["surface3"])
            fg = c["text"] if sel else (c["text"] if (self._hover == i and on) else c["muted"])
            if not on and not sel:
                fg = c["line2"]
            self.create_text(pos + sw // 2, h // 2, text=lab, fill=fg,
                             font=T.font(self._fsize, "bold" if sel else "normal"))
            pos += sw

    def recolor(self):
        self.draw()


# =========================================================================== 开关
class Toggle(Canvas):
    def __init__(self, parent, variable: tk.BooleanVar, command=None, **kw):
        self.var, self.command = variable, command
        self._tw, self._h = T.px(44), T.px(24)
        super().__init__(parent, width=self._tw, height=self._h, cursor="hand2", takefocus=1, **kw)
        self.enabled = True
        self.bind("<Button-1>", self._toggle)
        self.bind("<Key-space>", self._toggle)
        variable.trace_add("write", lambda *a: self.draw())
        self.draw()

    def _toggle(self, _e=None):
        if not self.enabled:
            return
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def set_enabled(self, on):
        self.enabled = on
        self.configure(cursor="hand2" if on else "arrow")
        self.draw()

    def draw(self):
        self.delete("all")
        c = T.c
        bg = bg_of(self.master)
        self.configure(bg=bg)
        on = bool(self.var.get())
        track = c["accent"] if on else c["line2"]
        if not self.enabled:
            track = mix(track, bg, 0.5)
        rounded(self, 0, 0, self._tw, self._h, self._h // 2, track, None, 1, bg)
        d = self._h - T.px(6)
        x = self._tw - T.px(3) - d if on else T.px(3)
        rounded(self, x, T.px(3), x + d, T.px(3) + d, d // 2, "#ffffff", None, 1, track)

    def recolor(self):
        self.draw()


# =========================================================================== 进度条
class Progress(Canvas):
    def __init__(self, parent, height=6, **kw):
        self._h = T.px(height)
        super().__init__(parent, height=self._h, **kw)
        self.value = 0.0
        self._indet = None
        self._phase = 0.0
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, value: float):
        self.stop()
        self.value = max(0.0, min(1.0, value))
        self.draw()

    def start(self):
        """不确定进度：一段在轨道上来回滑动的高亮。"""
        if self._indet is None:
            self._tick()

    def stop(self):
        if self._indet is not None:
            self.after_cancel(self._indet)
            self._indet = None

    def _tick(self):
        self._phase = (self._phase + 0.018) % 1.0
        self.draw()
        self._indet = self.after(30, self._tick)

    def draw(self):
        self.delete("all")
        c = T.c
        bg = bg_of(self.master)
        self.configure(bg=bg)
        w, h = self.winfo_width(), self._h
        if w <= 1:
            return
        rounded(self, 0, 0, w, h, h // 2, c["surface3"], None, 1, bg)
        if self._indet is not None:
            seg = w * 0.28
            x = (w + seg) * (0.5 - 0.5 * math.cos(self._phase * 2 * math.pi)) - seg
            x0, x1 = max(0, x), min(w, x + seg)
            if x1 - x0 >= h:
                rounded(self, x0, 0, x1, h, h // 2, c["accent"], None, 1, c["surface3"])
        elif self.value > 0:
            rounded(self, 0, 0, max(h, w * self.value), h, h // 2, c["accent"], None, 1, c["surface3"])

    def recolor(self):
        self.draw()


# =========================================================================== 输入框
class Field(Canvas):
    """圆角输入框：内部是无边框的 tk.Entry，聚焦时描边变成主色。"""

    def __init__(self, parent, variable: tk.StringVar, placeholder="", mono=False, show=None,
                 readonly=False, height=40, size=10, **kw):
        self._h = T.px(height)
        super().__init__(parent, height=self._h, **kw)
        self.var, self.placeholder, self.readonly = variable, placeholder, readonly
        self._focus = False
        self.enabled = True
        self.entry = tk.Entry(self, textvariable=variable, bd=0, highlightthickness=0, relief="flat",
                              font=T.font(size, "normal", mono), show=show or "")
        self._ph = tk.Label(self, text=placeholder, font=T.font(size), anchor="w", bd=0, padx=0,
                            cursor="xterm")
        self._ph.bind("<Button-1>", lambda e: self.entry.focus_set())
        self._win = self.create_window(T.px(12), self._h // 2, window=self.entry, anchor="w")
        self._phwin = self.create_window(T.px(13), self._h // 2, window=self._ph, anchor="w")
        self.entry.bind("<FocusIn>", lambda e: self._setfocus(True), add="+")
        self.entry.bind("<FocusOut>", lambda e: self._setfocus(False), add="+")
        variable.trace_add("write", lambda *a: self._update_ph())
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        if readonly:
            self.entry.configure(state="readonly")
        self._apply_colors()

    def _setfocus(self, on):
        self._focus = on
        self.draw()
        self._update_ph()

    def _update_ph(self):
        show = bool(self.placeholder) and not self.var.get() and not self._focus
        self.itemconfigure(self._phwin, state="normal" if show else "hidden")

    def set_enabled(self, on: bool):
        self.enabled = on
        self.entry.configure(state="normal" if on and not self.readonly else
                             ("readonly" if on else "disabled"))
        self._apply_colors()
        self.draw()

    def _apply_colors(self):
        c = T.c
        fill = c["surface2"] if self.enabled else c["surface3"]
        fg = c["text"] if self.enabled else c["muted"]
        self.entry.configure(bg=fill, fg=fg, insertbackground=c["text"], disabledbackground=fill,
                             disabledforeground=c["muted"], readonlybackground=fill,
                             selectbackground=c["accent_soft_hover"], selectforeground=c["text"])
        self._ph.configure(bg=fill, fg=c["muted"])

    def draw(self):
        self.delete("shape")
        w, h = self.winfo_width(), self._h
        if w <= 1:
            return
        bg = bg_of(self.master)
        self.configure(bg=bg)
        c = T.c
        fill = c["surface2"] if self.enabled else c["surface3"]
        line = c["focus"] if self._focus else c["line"]
        bw = max(2, T.px(2)) if self._focus else max(1, T.px(1))
        rounded(self, 0, 0, w, h, T.px(10), fill, line, bw, bg, tags=("shape",))
        self.tag_lower("shape")
        self.itemconfigure(self._win, width=max(10, w - T.px(24)))
        self._update_ph()

    def recolor(self):
        self._apply_colors()
        self.draw()


class Stepper(tk.Frame, Themed):
    """数字输入：[ − ] 30 [ + ]"""

    def __init__(self, parent, variable: tk.StringVar, lo=0, hi=9999, step=1, width=64):
        super().__init__(parent, bg=bg_of(parent), highlightthickness=0)
        self.var, self.lo, self.hi, self.step = variable, lo, hi, step
        self.minus = IconButton(self, "minus", lambda: self._bump(-1), size=30, kind="secondary")
        self.minus.pack(side="left")
        self.field = Field(self, variable, width=T.px(width), height=30)
        self.field.entry.configure(justify="center")
        self.field.pack(side="left", padx=T.px(6))
        self.plus = IconButton(self, "plus", lambda: self._bump(1), size=30, kind="secondary")
        self.plus.pack(side="left")

    def _bump(self, d):
        try:
            v = float(self.var.get() or 0)
        except ValueError:
            v = self.lo
        v = max(self.lo, min(self.hi, v + d * self.step))
        self.var.set(str(int(v)) if v == int(v) else f"{v:g}")

    def set_enabled(self, on):
        for b in (self.minus, self.plus):
            b.set_state("normal" if on else "disabled")
        self.field.set_enabled(on)

    def recolor(self):
        self.configure(bg=bg_of(self.master))


class Select(Canvas):
    """下拉选择：点开是一个菜单。options: [(value, label)]"""

    def __init__(self, parent, variable: tk.StringVar, options, height=36, width=220, **kw):
        self._h = T.px(height)
        super().__init__(parent, height=self._h, width=T.px(width), cursor="hand2", **kw)
        self.var, self.options = variable, list(options)
        self._hover = False
        self.enabled = True
        self.bind("<Button-1>", self._open)
        self.bind("<Enter>", lambda e: self._sethover(True))
        self.bind("<Leave>", lambda e: self._sethover(False))
        self.bind("<Configure>", lambda e: self.draw())
        variable.trace_add("write", lambda *a: self.draw())

    def set_options(self, options):
        self.options = list(options)
        self.draw()

    def _sethover(self, on):
        self._hover = on
        self.draw()

    def _open(self, _e=None):
        if not self.enabled:
            return
        c = T.c
        menu = tk.Menu(self, tearoff=0, bg=c["surface"], fg=c["text"], activebackground=c["accent_soft"],
                       activeforeground=c["text"], bd=1, relief="solid", font=T.font(10))
        for val, lab in self.options:
            menu.add_command(label=lab, command=lambda v=val: self.var.set(v))
        try:
            menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self._h + T.px(2))
        finally:
            menu.grab_release()

    def draw(self):
        self.delete("all")
        c = T.c
        bg = bg_of(self.master)
        self.configure(bg=bg)
        w, h = self.winfo_width(), self._h
        if w <= 1:
            w = int(self.cget("width"))
        fill = c["hover"] if self._hover else c["surface2"]
        rounded(self, 0, 0, w, h, T.px(10), fill, c["line"], max(1, T.px(1)), bg)
        lab = next((lab for v, lab in self.options if v == self.var.get()), self.var.get())
        self.create_text(T.px(12), h // 2, text=lab, fill=c["text"], font=T.font(10), anchor="w")
        isz = T.px(14)
        self.create_image(w - T.px(10), h // 2, image=icon_image("chevron-down", isz, c["muted"], fill),
                          anchor="e")

    def recolor(self):
        self.draw()


# =========================================================================== 杂项
class Pill(Canvas):
    """小标签：kind = accent / muted / warn / danger"""

    def __init__(self, parent, text="", kind="muted", dot=False, **kw):
        self._h = T.px(22)
        super().__init__(parent, height=self._h, **kw)
        self.text, self.kind, self.dot = text, kind, dot
        self.set(text, kind)

    def set(self, text=None, kind=None):
        import tkinter.font as tkfont
        if text is not None:
            self.text = text
        if kind is not None:
            self.kind = kind
        w = tkfont.Font(font=T.font(9, "bold")).measure(self.text) + T.px(20) + (T.px(12) if self.dot else 0)
        self.configure(width=w)
        self.draw()

    def draw(self):
        self.delete("all")
        c = T.c
        bg = bg_of(self.master)
        self.configure(bg=bg)
        fill, fg = {"accent": (c["accent_soft"], c["accent_text"]), "warn": (c["warn_soft"], c["warn"]),
                    "danger": (c["danger_soft"], c["danger"])}.get(self.kind, (c["surface3"], c["text2"]))
        w, h = int(self.cget("width")), self._h
        rounded(self, 0, 0, w, h, h // 2, fill, None, 1, bg)
        x = T.px(10)
        if self.dot:
            d = T.px(7)
            dotc = {"accent": c["accent"], "warn": c["warn"], "danger": c["danger"]}.get(self.kind, c["muted"])
            rounded(self, x, (h - d) // 2, x + d, (h - d) // 2 + d, d // 2, dotc, None, 1, fill)
            x += d + T.px(5)
        self.create_text(x, h // 2, text=self.text, fill=fg, font=T.font(9, "bold"), anchor="w")

    def recolor(self):
        self.draw()


class Icon(Canvas):
    """单独的图标，可选圆角底块（用于文件类型、页面标题）。"""

    def __init__(self, parent, name, size=20, color="text2", tile=None, tile_size=None, radius=10, **kw):
        self.name, self.size, self.color, self.tile = name, T.px(size), color, tile
        self.tile_size = T.px(tile_size) if tile_size else self.size
        self.radius = T.px(radius)
        super().__init__(parent, width=self.tile_size, height=self.tile_size, **kw)
        self.draw()

    def set(self, name=None, color=None, tile=None):
        if name:
            self.name = name
        if color:
            self.color = color
        if tile:
            self.tile = tile
        self.draw()

    def _col(self, v):
        return T.c.get(v, v) if v else None

    def draw(self):
        self.delete("all")
        bg = bg_of(self.master)
        self.configure(bg=bg)
        s = self.tile_size
        under = bg
        if self.tile:
            under = self._col(self.tile)
            rounded(self, 0, 0, s, s, self.radius, under, None, 1, bg)
        self.create_image(s // 2, s // 2, image=icon_image(self.name, self.size, self._col(self.color), under))

    def recolor(self):
        self.draw()


class ScrollFrame(tk.Frame, Themed):
    """可滚动区域：.body 放内容；细滚动条只在内容超出时出现；滚轮在区域内有效。"""

    def __init__(self, parent, bg="bg"):
        super().__init__(parent, bg=T.c[bg], highlightthickness=0)
        self._roles = {"bg": bg}
        self.canvas = tk.Canvas(self, bg=T.c[bg], highlightthickness=0, bd=0)
        self.canvas._roles = {"bg": bg}
        self.body = frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.bar = ScrollBar(self, self.canvas)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.configure(yscrollcommand=self.bar.set)
        for w in (self.canvas, self.body):
            w.bind("<Enter>", self._bind_wheel)
            w.bind("<Leave>", self._unbind_wheel)

    def _on_body(self, _e=None):
        self.canvas.configure(scrollregion=(0, 0, self.body.winfo_reqwidth(), self.body.winfo_reqheight()))
        self._update_bar()

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        self._update_bar()

    def _update_bar(self):
        need = self.body.winfo_reqheight() > self.canvas.winfo_height() + 1
        if need and not self.bar.winfo_ismapped():
            self.bar.place(relx=1.0, x=-T.px(2), y=T.px(2), relheight=1.0, height=-T.px(4), anchor="ne")
        elif not need and self.bar.winfo_ismapped():
            self.bar.place_forget()
            self.canvas.yview_moveto(0)

    def _bind_wheel(self, _e=None):
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", lambda e: self._scroll(-3))
        self.canvas.bind_all("<Button-5>", lambda e: self._scroll(3))

    def _unbind_wheel(self, _e=None):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _wheel(self, e):
        import sys
        delta = -e.delta if sys.platform == "darwin" else -e.delta // 40
        self._scroll(delta)

    def _scroll(self, units):
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(units), "units")

    def recolor(self):
        pass


class ScrollBar(Canvas):
    def __init__(self, parent, target: tk.Canvas):
        super().__init__(parent, width=T.px(10))
        self.target = target
        self.lo, self.hi = 0.0, 1.0
        self._drag = None
        self._hover = False
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.bind("<Enter>", lambda e: self._sethover(True))
        self.bind("<Leave>", lambda e: self._sethover(False))

    def _sethover(self, on):
        self._hover = on
        self.draw()

    def set(self, lo, hi):
        self.lo, self.hi = float(lo), float(hi)
        self.draw()

    def _press(self, e):
        h = self.winfo_height()
        if self.lo * h <= e.y <= self.hi * h:
            self._drag = (e.y, self.lo)
        else:
            self.target.yview_moveto(max(0.0, e.y / h - (self.hi - self.lo) / 2))

    def _motion(self, e):
        if self._drag:
            y0, lo0 = self._drag
            self.target.yview_moveto(lo0 + (e.y - y0) / max(1, self.winfo_height()))

    def draw(self):
        self.delete("all")
        bg = bg_of(self.master)
        self.configure(bg=bg)
        w, h = self.winfo_width(), self.winfo_height()
        if h <= 1:
            return
        bw = T.px(6) if self._hover else T.px(4)
        x0 = (w - bw) // 2
        y0, y1 = int(self.lo * h), int(self.hi * h)
        if y1 - y0 < bw * 3:
            y1 = y0 + bw * 3
        rounded(self, x0, y0, x0 + bw, y1, bw // 2, T.c["line2"] if not self._hover else T.c["muted"],
                None, 1, bg)

    def recolor(self):
        self.draw()


class Toast:
    """窗口底部的短暂提示（替代「已复制」之类的弹窗）。"""

    def __init__(self, root):
        self.root = root
        self.cv = None
        self._job = None

    def show(self, text: str, kind: str = "ok", ms: int = 2200):
        self.hide()
        c = T.c
        import tkinter.font as tkfont
        font = T.font(10, "bold")
        tw = tkfont.Font(font=font).measure(text)
        ic = T.px(16)
        w, h = tw + ic + T.px(44), T.px(40)
        self.root.update_idletasks()
        bg = visible_color_at(self.root, self.root.winfo_width() // 2 - w // 2 + 2,
                              self.root.winfo_height() - T.px(24) - h // 2)
        fill = c["text"]
        fg = c["bg"]
        self.cv = tk.Canvas(self.root, width=w, height=h, bg=bg, highlightthickness=0, bd=0)
        rounded(self.cv, 0, 0, w, h, h // 2, fill, None, 1, bg)
        name = {"ok": "check-circle", "err": "alert", "info": "info"}.get(kind, "info")
        col = {"ok": c["accent"], "err": c["danger"]}.get(kind, fg)
        self.cv.create_image(T.px(16), h // 2, image=icon_image(name, ic, col, fill), anchor="w")
        self.cv.create_text(T.px(16) + ic + T.px(10), h // 2, text=text, fill=fg, font=font, anchor="w")
        self.cv.place(relx=0.5, rely=1.0, y=-T.px(24), anchor="s")
        self.cv.tk.call("raise", self.cv._w)       # Canvas.lift 被重载成了 tag_raise
        self._job = self.root.after(ms, self.hide)

    def hide(self):
        if self._job:
            self.root.after_cancel(self._job)
            self._job = None
        if self.cv is not None:
            self.cv.destroy()
            self.cv = None


class QRCode(Canvas):
    """二维码：白底圆角块（深色主题下也保持白底，保证能扫）。"""

    def __init__(self, parent, text="", size=168, **kw):
        self.size = T.px(size)
        super().__init__(parent, width=self.size, height=self.size, **kw)
        self.text = text
        self.draw()

    def set(self, text: str):
        self.text = text
        self.draw()

    def draw(self):
        self.delete("all")
        bg = bg_of(self.master)
        self.configure(bg=bg)
        s = self.size
        rounded(self, 0, 0, s, s, T.px(14), "#ffffff", T.c["line"], max(1, T.px(1)), bg)
        if not self.text:
            return
        from landrop.qr import encode
        try:
            m = encode(self.text, "M")
        except ValueError:
            return
        n = len(m)
        cell = max(1, (s - T.px(20)) // n)
        off = (s - cell * n) // 2
        for y, row in enumerate(m):
            x = 0
            while x < n:                       # 同一行连续的深色模块合并成一个矩形
                if row[x]:
                    x1 = x
                    while x1 < n and row[x1]:
                        x1 += 1
                    self.create_rectangle(off + x * cell, off + y * cell, off + x1 * cell,
                                          off + (y + 1) * cell, fill="#111318", width=0)
                    x = x1
                else:
                    x += 1

    def recolor(self):
        self.draw()


class Check(Canvas):
    """复选框（圆角方块 + 对勾）。"""

    def __init__(self, parent, variable: tk.BooleanVar, command=None, **kw):
        self.var, self.command = variable, command
        self._s = T.px(18)
        super().__init__(parent, width=self._s, height=self._s, cursor="hand2", **kw)
        self.bind("<Button-1>", self._toggle)
        variable.trace_add("write", lambda *a: self.draw())
        self.draw()

    def _toggle(self, _e=None):
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def draw(self):
        self.delete("all")
        c = T.c
        bg = bg_of(self.master)
        self.configure(bg=bg)
        s = self._s
        if self.var.get():
            rounded(self, 0, 0, s, s, T.px(5), c["accent"], None, 1, bg)
            self.create_image(s // 2, s // 2, image=icon_image("check", T.px(14), c["accent_ink"], c["accent"],
                                                                stroke=3))
        else:
            rounded(self, 0, 0, s, s, T.px(5), c["surface"], c["line2"], max(1, T.px(1.5)), bg)

    def recolor(self):
        self.draw()


def elide(text: str, limit: int = 56) -> str:
    """过长的文件名从中间省略，保留扩展名。"""
    if len(text) <= limit:
        return text
    keep = limit - 1
    return text[:keep - keep // 3] + "…" + text[-(keep // 3):]


def visible_color_at(root, x, y) -> str:
    """窗口内某点实际看到的颜色：自绘卡片 / 按钮画的是圆角块，背景色并不是它的 bg 属性。"""
    w = root.winfo_containing(root.winfo_rootx() + x, root.winfo_rooty() + y)
    while w is not None:
        role = getattr(w, "fill_role", None)
        if role:
            return T.c[role]
        roles = getattr(w, "_roles", None)
        if roles and roles.get("bg"):
            return T.c[roles["bg"]]
        w = w.master
    return T.c["bg"]


def hline(parent, bg_role="line", pady=0):
    f = frame(parent, bg=bg_role, height=max(1, T.px(1)))
    f.pack(fill="x", pady=pady)
    return f
