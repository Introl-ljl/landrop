"""Shared paths, addresses, file versions and console helpers. Standard library only."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import socket
import sys
import threading
from urllib.parse import parse_qs, urlsplit

CHUNK = 8 * 1024 * 1024


class CliError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Cancelled(CliError):
    pass


def setup_console():
    """Windows 控制台默认是 GBK 等代码页：强制 UTF-8 输出，避免中文文件名/提示乱码或崩溃。
    窗口版没有控制台（stdout 为 None）时什么也不做。"""
    for stream in (sys.stdout, sys.stderr):
        try:      # 行缓冲：输出接到管道 / 被脚本读取时，文件码要立刻出现，而不是等进程结束
            stream.reconfigure(encoding="utf-8", errors="replace",  # type: ignore[attr-defined]
                               line_buffering=True)
        except Exception:  # noqa: BLE001
            pass


PORTABLE_MARKER = "portable.txt"


def app_dir() -> str | None:
    """打包版的应用目录（可执行文件所在目录）；源码 / pyz 运行时返回 None。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.realpath(sys.executable))
    return None


def portable_dir() -> str | None:
    """免安装版：应用目录里有 portable.txt 时，数据跟着应用目录走（U 盘拷走即用）。

    macOS 不支持：.app 内部不可写（App Translocation / 签名），免安装版仍用系统目录。"""
    d = app_dir()
    if d and sys.platform != "darwin" and os.path.isfile(os.path.join(d, PORTABLE_MARKER)):
        return d
    return None


def default_data_dir() -> str:
    """The desktop and local CLI share one private data directory."""
    portable = portable_dir()
    if portable:
        return os.path.join(portable, "data")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LANDrop")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/LANDrop")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "landrop")


def resolve_data_dir(explicit: str | None = None) -> tuple[str, str]:
    """决定数据目录，返回 (目录, 来源说明)。优先级：
    显式参数 → 环境变量 LANDROP_DATA_DIR → 旧版习惯的 ./data（已有状态库时）→ 平台默认目录。"""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit)), "参数"
    env = os.environ.get("LANDROP_DATA_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env)), "LANDROP_DATA_DIR"
    legacy = os.path.abspath("data")
    if any(os.path.isfile(os.path.join(legacy, name)) for name in
           ("shares.sqlite3", "state.sqlite3")) and not portable_dir():
        return legacy, "当前目录已有的 ./data（旧版默认位置）"
    return default_data_dir(), "默认位置"


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
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "http://" + url
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or
                parsed.username or parsed.password or parsed.path not in ("", "/") or
                parsed.query or any(ch.isspace() for ch in parsed.netloc) or
                (port is not None and not 1 <= port <= 65535)):
            raise ValueError
    except ValueError:
        raise CliError("访问地址格式不正确，请输入 IP:端口或分享链接") from None
    return f"{parsed.scheme}://{parsed.netloc}"


def make_link(base: str, code: str) -> str:
    return normalize_base(base) + "/#code=" + code


def parse_target(address: str, code: str = "") -> tuple[str, str]:
    raw = address.strip()
    found = re.search(r"https?://[^\s]+", raw)
    address = found.group(0) if found else raw
    parsed = urlsplit(address if "://" in address else "http://" + address)
    value = code or (parse_qs(parsed.fragment).get("code") or [""])[0]
    if not value:
        match = re.search(r"授权码\s*[:：]\s*([0-9]{6})(?![0-9])", raw)
        value = match.group(1) if match else ""
    if not re.fullmatch(r"[0-9]{6}", str(value)):
        raise CliError("请输入六位数字授权码或分享链接；旧式文件码不再支持")
    return normalize_base(address), str(value)


def default_download_dir() -> str:
    path = os.path.expanduser("~/Downloads")
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                path = os.path.expandvars(winreg.QueryValueEx(
                    key, "{374DE290-123F-4565-9164-39C4925E467B}")[0])
        except OSError:
            pass
    elif sys.platform != "darwin":
        config = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                              "user-dirs.dirs")
        try:
            with open(config, encoding="utf-8") as stream:
                for line in stream:
                    if line.startswith("XDG_DOWNLOAD_DIR="):
                        values = shlex.split(line.partition("=")[2])
                        if values:
                            path = os.path.expandvars(values[0])
        except (OSError, ValueError):
            pass
    return os.path.abspath(os.path.expanduser(path))


def lan_addresses() -> list[str]:
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as conn:
            conn.connect(("10.255.255.255", 1))
            found.append(conn.getsockname()[0])
    except OSError:
        pass
    def lookup():
        try:
            found.extend(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    worker.join(1)
    return list(dict.fromkeys(ip for ip in found if not ip.startswith("127."))) or ["127.0.0.1"]


def file_version(path: str) -> str:
    return stat_version(os.stat(path))


def stat_version(stat) -> str:
    # Windows stat/fstat disagree on ctime in newer Python; birthtime is consistent.
    stamp = getattr(stat, "st_birthtime_ns", stat.st_ctime_ns) if os.name == "nt" else stat.st_ctime_ns
    return hashlib.sha256(str((stat.st_dev, stat.st_ino, stat.st_size,
                               stat.st_mtime_ns, stamp)).encode()).hexdigest()[:32]


def collect_entries(paths: list[str], on_warning=None) -> list[tuple[str, str]]:
    """把要发送的文件/文件夹展开成 [(本地路径, 相对名)]：
    文件用文件名，文件夹保留「文件夹名/子路径」结构；重名自动区分。"""
    entries, seen, out = [], set(), []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            top = os.path.basename(p.rstrip("/\\")) or "folder"
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    if os.path.isfile(full) and not os.path.islink(full):
                        entries.append((full, top + "/" + os.path.relpath(full, p).replace(os.sep, "/")))
        elif os.path.isfile(p):
            entries.append((p, os.path.basename(p)))
    for full, rel in entries:
        if rel in seen:
            original = rel
            parent = os.path.basename(os.path.dirname(full))
            alt = f"{parent}/{rel}" if parent else rel
            if alt in seen:
                stem, ext = os.path.splitext(rel)
                i = 1
                while f"{stem} ({i}){ext}" in seen:
                    i += 1
                alt = f"{stem} ({i}){ext}"
            rel = alt
            if on_warning:
                on_warning(f"重名：{original} → 以 {rel} 发送")
        seen.add(rel)
        out.append((full, rel))
    return out


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
    if len(name.encode("utf-8")) > 200:
        stem, ext = os.path.splitext(name)
        ext = ext.encode("utf-8")[:40].decode("utf-8", "ignore")
        name = stem.encode("utf-8")[:200 - len(ext.encode("utf-8"))].decode("utf-8", "ignore") + ext
    return name


def safe_relpath(rel: str) -> str:
    """把对端给的相对路径清洗成本地安全路径：逐段清洗，拒绝 ..、空段与绝对路径。"""
    raw = rel.replace("\\", "/")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if (not parts or len(parts) > 32 or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw)
            or any(p == ".." for p in parts)):
        raise CliError(f"不安全的路径：{rel!r}")
    return os.path.join(*[safe_local_name(p) for p in parts])
