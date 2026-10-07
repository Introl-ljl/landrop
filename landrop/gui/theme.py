# -*- coding: utf-8 -*-
"""Desktop colors and typography, shared with the restrained LAN browser UI."""
from __future__ import annotations

import os
import subprocess
import sys

LIGHT = {
    "bg": "#f5f7f6",
    "sidebar": "#edf0ef",
    "surface": "#ffffff",        # 卡片
    "surface2": "#f3f5f4",
    "surface3": "#e4eae6",
    "line": "#d9e1dd",
    "line2": "#c4cfc8",
    "text": "#18231e",
    "text2": "#3d4c43",
    "muted": "#617169",
    "accent": "#246b48",
    "accent_hover": "#1e603f",
    "accent_press": "#185334",
    "accent_ink": "#ffffff",
    "accent_text": "#246b48",
    "accent_soft": "#edf5ef",
    "accent_soft_hover": "#dfede3",
    "danger": "#d93541",
    "danger_soft": "#fbe4e6",
    "warn": "#b7791f",
    "warn_soft": "#fbf0dc",
    "hover": "#eaf0ed",
    "press": "#dce7e0",
    "nav_active": "#ffffff",
    "focus": "#346fc0",
    "shadow": "#dce3df",
}

DARK = {
    "bg": "#161b18",
    "sidebar": "#121713",
    "surface": "#202722",
    "surface2": "#252e28",
    "surface3": "#303b33",
    "line": "#39453d",
    "line2": "#49564d",
    "text": "#edf3ee",
    "text2": "#c8d4cd",
    "muted": "#b2beb6",
    "accent": "#80c89c",
    "accent_hover": "#9dd8b3",
    "accent_press": "#6fb689",
    "accent_ink": "#15221a",
    "accent_text": "#80c89c",
    "accent_soft": "#293d30",
    "accent_soft_hover": "#354c3d",
    "danger": "#ff6b74",
    "danger_soft": "#3a1d21",
    "warn": "#f2b84b",
    "warn_soft": "#3a2e17",
    "hover": "#2b362e",
    "press": "#364239",
    "nav_active": "#252e28",
    "focus": "#6c9bdc",
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
