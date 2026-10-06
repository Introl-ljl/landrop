# -*- coding: utf-8 -*-
"""桌面窗口的设计令牌：与网页（static/index.html「流光玻璃」）同一套配色，浅色 / 深色两套。

只依赖标准库。颜色都是不透明的十六进制值（Tk 不支持半透明），由网页里的 rgba 预先混合到底色上得到。
"""
from __future__ import annotations

import os
import subprocess
import sys

LIGHT = {
    "bg": "#f4f3f0",            # 内容区底色（暖白纸面）
    "sidebar": "#ebe9e4",
    "surface": "#ffffff",        # 卡片
    "surface2": "#f6f5f2",       # 输入框、次级填充
    "surface3": "#eceae5",       # 分段控件轨道、进度条轨道
    "line": "#e3e1db",
    "line2": "#d2cfc7",
    "text": "#15171c",
    "text2": "#464a53",
    "muted": "#70747d",
    "accent": "#0fc39d",
    "accent_hover": "#0db08d",
    "accent_press": "#0a9b7c",
    "accent_ink": "#032019",
    "accent_text": "#087a64",
    "accent_soft": "#dcf5ee",
    "accent_soft_hover": "#c9efe4",
    "danger": "#d93541",
    "danger_soft": "#fbe4e6",
    "warn": "#b7791f",
    "warn_soft": "#fbf0dc",
    "hover": "#efede8",
    "press": "#e6e4de",
    "nav_active": "#ffffff",
    "focus": "#0fc39d",
    "shadow": "#e6e4df",
}

DARK = {
    "bg": "#0d0f13",
    "sidebar": "#090a0d",
    "surface": "#16191f",
    "surface2": "#1c2027",
    "surface3": "#232833",
    "line": "#262b33",
    "line2": "#343a45",
    "text": "#eceef2",
    "text2": "#b8bec8",
    "muted": "#868c96",
    "accent": "#2fe6bd",
    "accent_hover": "#5cf0cf",
    "accent_press": "#22c9a4",
    "accent_ink": "#032019",
    "accent_text": "#5ceecd",
    "accent_soft": "#163b35",
    "accent_soft_hover": "#1c4a42",
    "danger": "#ff6b74",
    "danger_soft": "#3a1d21",
    "warn": "#f2b84b",
    "warn_soft": "#3a2e17",
    "hover": "#1d2128",
    "press": "#242932",
    "nav_active": "#1c2027",
    "focus": "#2fe6bd",
    "shadow": "#07080a",
}

# 文件类型配色（与网页 .k-* 一致）
KIND_COLORS = {
    "light": {"image": "#7c3aed", "video": "#db2777", "audio": "#d97706", "archive": "#92400e",
              "pdf": "#dc2626", "doc": "#2563eb", "sheet": "#16a34a", "slide": "#ea580c",
              "text": "#475569", "code": "#0e7490", "app": "#4f46e5", "folder": "#ca8a04",
              "other": "#6b7280"},
    "dark": {"image": "#a78bfa", "video": "#f472b6", "audio": "#fbbf24", "archive": "#d4a373",
             "pdf": "#f87171", "doc": "#60a5fa", "sheet": "#4ade80", "slide": "#fb923c",
             "text": "#94a3b8", "code": "#22d3ee", "app": "#818cf8", "folder": "#facc15",
             "other": "#9ca3af"},
}

_EXT_KIND = {}
for _kind, _exts in {
    "image": "jpg jpeg png gif webp bmp svg heic heif tif tiff ico raw",
    "video": "mp4 mkv mov avi webm flv wmv m4v",
    "audio": "mp3 wav flac aac ogg m4a wma opus",
    "archive": "zip rar 7z tar gz tgz bz2 xz zst iso dmg",
    "pdf": "pdf",
    "doc": "doc docx odt rtf pages",
    "sheet": "xls xlsx csv ods numbers",
    "slide": "ppt pptx odp key",
    "text": "txt md log ini cfg conf",
    "code": "py js ts json html css c cpp h java go rs sh yaml yml xml sql",
    "app": "exe msi apk deb rpm pkg app appimage",
}.items():
    for _e in _exts.split():
        _EXT_KIND[_e] = _kind


def file_kind(name: str, is_dir: bool = False) -> str:
    if is_dir:
        return "folder"
    return _EXT_KIND.get(os.path.splitext(name)[1].lstrip(".").lower(), "other")


class Theme:
    """当前主题：颜色、字体与缩放。窗口里只有一个实例（``theme.T``）。"""

    def __init__(self):
        self.mode = "light"          # 实际生效的 light / dark
        self.pref = "system"         # 用户选择：system / light / dark
        self.c = dict(LIGHT)
        self.scale = 1.0
        self.family = "TkDefaultFont"
        self.mono = "TkFixedFont"

    # ---- 缩放：Windows 高 DPI 下像素尺寸按比例放大（字体用磅值，Tk 自己会缩放）
    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    def kind_color(self, kind: str) -> str:
        return KIND_COLORS[self.mode].get(kind, KIND_COLORS[self.mode]["other"])

    def set_mode(self, pref: str):
        self.pref = pref if pref in ("system", "light", "dark") else "system"
        self.mode = system_dark_mode() if self.pref == "system" else self.pref
        self.c = dict(DARK if self.mode == "dark" else LIGHT)

    def init_tk(self, root):
        """在 Tk() 创建之后调用：确定缩放与字体。"""
        import tkinter.font as tkfont
        if sys.platform == "darwin":
            self.scale = 1.0                         # macOS 以「点」为单位，Retina 由系统处理
        else:
            self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        families = set(tkfont.families(root))
        self.family = _first(families, _UI_FONTS.get(sys.platform[:3], _UI_FONTS["lin"])) \
            or tkfont.nametofont("TkDefaultFont").actual("family")
        self.mono = _first(families, _MONO_FONTS) or tkfont.nametofont("TkFixedFont").actual("family")

    def font(self, size: int = 10, weight: str = "normal", mono: bool = False):
        # 字号按 Windows / Linux 调（Tk 缩放 ≈ 1.33，10 磅 ≈ 13 像素）；macOS 的 Tk 缩放是 1.0，放大 4/3 才一样大
        if sys.platform == "darwin":
            size = round(size * 4 / 3)
        return (self.mono if mono else self.family, size, weight)


_UI_FONTS = {
    "win": ["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"],
    "dar": ["PingFang SC", "Hiragino Sans GB", "Helvetica Neue"],
    "lin": ["Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
            "Sarasa UI SC", "Droid Sans Fallback", "Ubuntu", "Cantarell", "DejaVu Sans"],
}
_MONO_FONTS = ["Cascadia Mono", "JetBrains Mono", "SF Mono", "Menlo", "Consolas", "Ubuntu Mono",
               "Noto Sans Mono CJK SC", "Noto Sans Mono", "DejaVu Sans Mono"]


def _first(available, wanted):
    for name in wanted:
        if name in available:
            return name
    return None


def system_dark_mode() -> str:
    """读取系统的浅色 / 深色设置；读不到时用浅色。"""
    try:
        if sys.platform.startswith("win"):
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value else "dark"
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=2)
            return "dark" if "Dark" in out.stdout else "light"
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, text=True, timeout=2)
        if "dark" in out.stdout:
            return "dark"
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                             capture_output=True, text=True, timeout=2)
        return "dark" if "dark" in out.stdout.lower() else "light"
    except Exception:  # noqa: BLE001
        return "light"


def enable_dpi_awareness():
    """Windows：声明 DPI 感知，必须在创建 Tk() 之前调用；否则高分屏上整个窗口是糊的。"""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        try:      # 系统级 DPI 感知（与 IDLE 相同）：按主显示器缩放、清晰；拖到不同缩放的副屏时由系统拉伸
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def style_titlebar(root, dark: bool):
    """Windows 10/11：让标题栏跟随深色模式（DWMWA_USE_IMMERSIVE_DARK_MODE）。"""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):            # 20：Win10 2004+ / Win11；19：更早的 Win10
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:  # noqa: BLE001
        pass


T = Theme()
