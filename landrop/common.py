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
    """数据目录的唯一约定（窗口、``landrop serve``、``send --server`` 读密钥都用它）。"""
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
    if os.path.isfile(os.path.join(legacy, "state.sqlite3")) and not portable_dir():
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


def collect_entries(paths: list[str]) -> list[tuple[str, str]]:
    """把要发送的文件/文件夹展开成 [(本地路径, 相对名)]。直连与经服务器共用，行为一致：
    文件用文件名，文件夹保留「文件夹名/子路径」结构；重名只取第一个。"""
    entries, seen, out = [], set(), []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            top = os.path.basename(p.rstrip("/\\")) or "folder"
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    if os.path.isfile(full):
                        entries.append((full, top + "/" + os.path.relpath(full, p).replace(os.sep, "/")))
        elif os.path.isfile(p):
            entries.append((p, os.path.basename(p)))
    for full, rel in entries:
        if rel not in seen:
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
    return name


def safe_relpath(rel: str) -> str:
    """把对端给的相对路径清洗成本地安全路径：逐段清洗，拒绝 ..、空段与绝对路径。"""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise CliError(f"不安全的路径：{rel!r}")
    return os.path.join(*[safe_local_name(p) for p in parts])
