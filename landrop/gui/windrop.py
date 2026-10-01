# -*- coding: utf-8 -*-
"""Windows：把文件 / 文件夹拖进窗口（WM_DROPFILES）。仅标准库 ctypes，其他平台不加载。

做法：对 Tk 窗口调用 DragAcceptFiles，并替换它的窗口过程，收到 WM_DROPFILES 时取出路径，
再交回 Tk 的事件循环处理；其他消息原样交给原窗口过程。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

WM_DROPFILES = 0x0233
WM_COPYDATA = 0x004A
WM_COPYGLOBALDATA = 0x0049
MSGFLT_ALLOW = 1
GWLP_WNDPROC = -4

LRESULT = ctypes.c_ssize_t
LONG_PTR = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

_user32 = ctypes.windll.user32
_shell32 = ctypes.windll.shell32

_shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
_shell32.DragAcceptFiles.restype = None
_shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
_shell32.DragQueryFileW.restype = wintypes.UINT
_shell32.DragFinish.argtypes = [wintypes.HANDLE]
_shell32.DragFinish.restype = None
_user32.CallWindowProcW.argtypes = [LONG_PTR, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.CallWindowProcW.restype = LRESULT
_user32.GetParent.argtypes = [wintypes.HWND]
_user32.GetParent.restype = wintypes.HWND
if hasattr(_user32, "SetWindowLongPtrW"):          # 64 位
    _set_long = _user32.SetWindowLongPtrW
else:                                              # 32 位只有 SetWindowLongW
    _set_long = _user32.SetWindowLongW
_set_long.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
_set_long.restype = LONG_PTR

_keep = []      # 回调对象必须一直有引用，否则被回收后 Windows 调到野指针


def _files(hdrop) -> list[str]:
    n = _shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
    out = []
    for i in range(n):
        length = _shell32.DragQueryFileW(hdrop, i, None, 0)
        buf = ctypes.create_unicode_buffer(length + 1)
        _shell32.DragQueryFileW(hdrop, i, buf, length + 1)
        out.append(buf.value)
    return out


def enable(root, callback):
    """让 root 窗口接受拖放；callback(paths) 在 Tk 主循环里被调用。"""
    root.update_idletasks()
    targets = [root.winfo_id()]
    parent = _user32.GetParent(root.winfo_id())
    if parent:
        targets.append(parent)
    for hwnd in targets:
        try:            # 以管理员身份运行时，允许普通权限的资源管理器把文件拖进来
            for msg in (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA):
                _user32.ChangeWindowMessageFilterEx(hwnd, msg, MSGFLT_ALLOW, None)
        except Exception:  # noqa: BLE001
            pass
        _shell32.DragAcceptFiles(hwnd, True)
        old = [0]

        def proc(h, msg, wparam, lparam, old=old):
            if msg == WM_DROPFILES:
                try:
                    paths = _files(wparam)
                finally:
                    _shell32.DragFinish(wparam)
                if paths:
                    root.after(0, lambda p=paths: callback(p))
                return 0
            return _user32.CallWindowProcW(old[0], h, msg, wparam, lparam)

        cb = WNDPROC(proc)
        _keep.append(cb)
        old[0] = _set_long(hwnd, GWLP_WNDPROC, ctypes.cast(cb, ctypes.c_void_p).value)


def simulate_drop(hwnd: int, paths: list[str]):
    """测试用：构造一个 DROPFILES 内存块并投递 WM_DROPFILES，等同于从资源管理器拖入。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    data = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    header = 20                                     # DROPFILES: pFiles, pt.x, pt.y, fNC, fWide
    hglobal = kernel32.GlobalAlloc(0x0042, header + len(data))      # GHND
    ptr = kernel32.GlobalLock(hglobal)
    ctypes.memmove(ptr, (ctypes.c_uint32 * 5)(header, 0, 0, 0, 1), header)
    ctypes.memmove(ptr + header, data, len(data))
    kernel32.GlobalUnlock(hglobal)
    _user32.PostMessageW(hwnd, WM_DROPFILES, hglobal, 0)
