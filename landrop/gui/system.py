# -*- coding: utf-8 -*-
"""与操作系统打交道的部分：打开文件/网址、窗口设置的保存、把自带的命令行加到 PATH。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import webbrowser

from landrop.common import app_dir, default_data_dir, portable_dir

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def open_path(path: str):
    if IS_WIN:
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if IS_MAC else "xdg-open", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_url(url: str):
    webbrowser.open(url)


# --------------------------------------------------------------------------- 设置
def settings_path() -> str:
    base = os.environ.get("LANDROP_DATA_DIR") or default_data_dir()
    return os.path.join(base, "desktop.json")


def load_settings() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict):
    path = settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def default_download_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


# --------------------------------------------------------------------------- 命令行工具
def bundled_cli() -> str | None:
    """打包版里与窗口同目录的命令行可执行文件；源码 / pyz 运行时为 None。"""
    d = app_dir()
    if not d:
        return None
    path = os.path.join(d, "landrop.exe" if IS_WIN else "landrop")
    return path if os.path.isfile(path) else None


def _win_user_path() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, "Path")[0]
    except OSError:
        return ""


def _win_system_path() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as k:
            return winreg.QueryValueEx(k, "Path")[0]
    except OSError:
        return ""


def _win_has(paths: str, d: str) -> bool:
    want = os.path.normcase(os.path.normpath(d))
    return any(os.path.normcase(os.path.normpath(os.path.expandvars(p))) == want
               for p in paths.split(";") if p.strip())


def _link_target() -> str:
    if IS_MAC:
        return "/usr/local/bin/landrop"
    return os.path.join(os.path.expanduser("~"), ".local", "bin", "landrop")


def cli_status() -> dict:
    """命令行工具的状态，给「设置」页显示。

    返回 {available, installed, location, how, can_toggle, note}：
      available  这个形态是否自带命令行（打包版）；
      installed  终端里能否直接用 landrop；
      location   找到的 landrop 位置；
      can_toggle 能否由窗口一键添加 / 移除（安装包装的由安装包管理）。
    """
    cli = bundled_cli()
    found = shutil.which("landrop")
    if not cli:
        return {"available": False, "installed": bool(found), "location": found or "",
                "can_toggle": False,
                "note": "当前从源码 / pyz 运行：命令行用法为 python -m landrop，或 pip install . 得到 landrop 命令。"}
    d = os.path.dirname(cli)
    if IS_WIN:
        user, system = _win_user_path(), _win_system_path()
        in_user, in_system = _win_has(user, d), _win_has(system, d)
        installed = in_user or in_system or bool(found and os.path.dirname(found) == d)
        return {"available": True, "installed": installed, "location": cli if installed else "",
                "can_toggle": not in_system,
                "note": "新打开的终端里生效。" if installed else
                        "添加后在新打开的终端（cmd / PowerShell）里输入 landrop 即可使用。"}
    link = _link_target()
    linked = os.path.islink(link) and os.path.realpath(link) == os.path.realpath(cli)
    other = found and os.path.realpath(found) == os.path.realpath(cli)
    installed = bool(linked or other)
    managed = bool(other and not linked)            # 由安装包（.deb / .pkg）放好的
    note = ""
    if not IS_MAC and not installed:
        note = f"会在 {link} 创建链接。"
    elif not IS_MAC and linked and os.path.dirname(link) not in os.environ.get("PATH", "").split(os.pathsep):
        note = f"{os.path.dirname(link)} 不在 PATH 中，请把它加入 PATH。"
    return {"available": True, "installed": installed, "location": found or (link if linked else ""),
            "can_toggle": not managed, "note": note}


def _win_broadcast_env():
    try:
        import ctypes
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x1A, 0x2
        res = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
                                                 SMTO_ABORTIFHUNG, 3000, ctypes.byref(res))
    except Exception:  # noqa: BLE001
        pass


def install_cli() -> str:
    """把命令行加入 PATH；返回给用户看的结果说明。失败抛 OSError。"""
    cli = bundled_cli()
    if not cli:
        raise OSError("当前不是打包版，没有自带的命令行可执行文件")
    if IS_WIN:
        import winreg
        d = os.path.dirname(cli)
        paths = _win_user_path()
        if not _win_has(paths, d):
            new = (paths.rstrip(";") + ";" if paths else "") + d
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, new)
            _win_broadcast_env()
        return "已添加到 PATH：新打开的终端里输入 landrop 即可使用"
    link = _link_target()
    if IS_MAC:
        script = (f'mkdir -p /usr/local/bin && ln -sf "{cli}" "{link}"')
        if os.access(os.path.dirname(link), os.W_OK):
            subprocess.run(["/bin/sh", "-c", script], check=True)
        else:                       # 需要管理员权限：系统会弹出密码框
            subprocess.run(["osascript", "-e",
                            f'do shell script "{script.replace(chr(34), chr(92) + chr(34))}" '
                            "with administrator privileges"], check=True)
        return f"已创建 {link}"
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(cli, link)
    return f"已创建 {link}"


def uninstall_cli() -> str:
    cli = bundled_cli()
    if IS_WIN:
        import winreg
        if not cli:
            return "没有需要移除的内容"
        d = os.path.dirname(cli)
        kept = [p for p in _win_user_path().split(";") if p.strip() and not _win_has(p, d)]
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(kept))
        _win_broadcast_env()
        return "已从 PATH 移除"
    link = _link_target()
    if os.path.islink(link):
        if IS_MAC and not os.access(os.path.dirname(link), os.W_OK):
            subprocess.run(["osascript", "-e", f'do shell script "rm -f {link}" with administrator privileges'],
                           check=True)
        else:
            os.remove(link)
    return f"已移除 {link}"


def install_kind() -> str:
    """portable / installed / source —— 显示在「设置 · 关于」里。"""
    if portable_dir():
        return "portable"
    return "installed" if app_dir() else "source"
