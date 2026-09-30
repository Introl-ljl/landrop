"""共用小工具：错误类型、控制台、文件码、路径清洗、进度显示。仅标准库。"""
from __future__ import annotations

import hashlib
import os
import re
import sys

CHUNK = 8 * 1024 * 1024


class CliError(Exception):
    pass


def setup_console():
    """Windows 控制台默认是 GBK 等代码页：强制 UTF-8 输出，避免中文文件名/提示乱码或崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def default_data_dir() -> str:
    """与 desktop.py 的数据目录约定一致，便于本机直接读到 admin-key.txt。"""
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LANDrop")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/LANDrop")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "landrop")


def cmd_prefix() -> str:
    """生成给接收端复制的命令前缀：打包版/已安装用 landrop，pyz 与源码运行按平台选解释器。"""
    if getattr(sys, "frozen", False) or os.environ.get("LANDROP_INSTALLED"):
        return "landrop"
    py = "python" if sys.platform.startswith("win") else "python3"
    if sys.argv and sys.argv[0].endswith(".pyz"):
        return f"{py} {os.path.basename(sys.argv[0])}"
    return f"{py} -m landrop"


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def progress(label: str, done: int, total: int):
    if not sys.stderr.isatty():
        return
    pct = 100 if total == 0 else done * 100 // total
    sys.stderr.write(f"\r  {label[:40]:<40} {pct:3d}%  {human(done)}/{human(total)}   ")
    sys.stderr.flush()


def progress_end():
    if sys.stderr.isatty():
        sys.stderr.write("\n")


def normalize_base(url: str) -> str:
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url):
        url = "http://" + url
    return url


def make_code(base: str, token: str) -> str:
    """文件码 = 服务地址(去掉 http://) + '/' + 令牌；https 地址保留协议前缀。"""
    host = base[len("http://"):] if base.startswith("http://") else base
    return f"{host}/{token}"


def parse_code(code: str) -> tuple[str, str]:
    code = code.strip()
    m = re.match(r"^(https?://)?([^/\s]+)/([A-Za-z0-9_-]+)$", code)
    if not m:
        raise CliError("文件码格式不对，应形如 192.168.1.5:8000/令牌")
    return (m.group(1) or "http://") + m.group(2), m.group(3)


def extract_code(text: str) -> str:
    """从粘贴内容里抠出文件码：整条取件命令、带引号的、多余空白都能认。"""
    m = re.search(r"(?:https?://)?[\w.\-\[\]:]+:\d+/[A-Za-z0-9_-]+", text or "")
    return m.group(0) if m else (text or "").strip()


def unique_path(directory: str, name: str) -> str:
    stem, ext = os.path.splitext(name)
    candidate, i = os.path.join(directory, name), 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem} ({i}){ext}")
        i += 1
    return candidate


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def safe_local_name(name: str) -> str:
    """服务端给的名字不可信：去路径、去 Windows 非法字符/保留名，三个平台都能落盘。"""
    name = name.replace("\\", "/").split("/")[-1]   # 不用 os.path.basename：Windows 上会把 "a:b" 当盘符
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f")
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip().rstrip(". ")
    if not name:
        name = "download"
    if name.split(".")[0].upper() in _WIN_RESERVED:
        name = "_" + name
    return name


def safe_relpath(rel: str) -> str:
    """把对端给的相对路径清洗成本地安全路径：逐段清洗，拒绝 ..、空段与绝对路径。"""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise CliError(f"不安全的路径：{rel!r}")
    return os.path.join(*[safe_local_name(p) for p in parts])
