#!/usr/bin/env python3
"""把 PyInstaller 产物做成每个平台的「安装版 + 免安装版」。先运行 ``pyinstaller landrop.spec``。

    python packaging/package.py [--out dist/release] [--arch x86_64|arm64]

产物（<v> 为 landrop.__version__）::

    Windows  LANDrop-<v>-windows-x86_64-setup.exe     Inno Setup 安装程序（可选加入 PATH）
             LANDrop-<v>-windows-x86_64-portable.zip  解压即用，数据存放在程序目录 data/
    macOS    LANDrop-<v>-macos-<arch>-setup.pkg       装到 /Applications，并链接 /usr/local/bin/landrop
             LANDrop-<v>-macos-<arch>-portable.zip    LAN Drop.app，拖到任意位置运行
    Linux    LANDrop-<v>-linux-<arch>-setup.deb       /opt/landrop + /usr/bin/landrop(-gui) + 桌面菜单
             LANDrop-<v>-linux-<arch>-portable.tar.gz 解压即用，数据存放在程序目录 data/

每个产物都同时包含图形界面与命令行（同一份运行时）。
"""
from __future__ import annotations

import argparse
import os
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from landrop import __version__  # noqa: E402
from landrop.common import PORTABLE_MARKER, setup_console  # noqa: E402

DIST = os.path.join(ROOT, "dist")
BUNDLE = os.path.join(DIST, "LANDrop")
ASSETS = os.path.join(ROOT, "packaging", "assets")
APP_ID = "io.github.introl-ljl.landrop"

PORTABLE_NOTE = """LAN Drop 免安装版 / portable

本文件存在时，LAN Drop 把数据（收集服务的状态库、文件、管理员密钥）保存在同目录的 data/ 下，
整个文件夹拷到其他电脑或 U 盘即可继续使用。删除本文件则改用系统默认数据目录。

While this file exists, LAN Drop keeps its data in ./data next to the program,
so the whole folder can be carried around. Delete it to use the per-user data directory instead.

命令行 / CLI:  landrop --help   （Windows: landrop.exe --help）
"""


def run(cmd, **kw):
    print("+", " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def detect_arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(m, m)


def artifact(os_name: str, arch: str, kind: str, ext: str) -> str:
    return f"LANDrop-{__version__}-{os_name}-{arch}-{kind}{ext}"


def zip_dir(src: str, dest: str, arcroot: str, extra: dict | None = None):
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for base, _dirs, files in os.walk(src):
            for fn in files:
                full = os.path.join(base, fn)
                zf.write(full, os.path.join(arcroot, os.path.relpath(full, src)))
        for name, text in (extra or {}).items():
            zf.writestr(os.path.join(arcroot, name), text)


# --------------------------------------------------------------------------- Windows
def find_iscc() -> str:
    for ver in ("7", "6"):                 # 新版优先：7.0.2+ 自带简体中文界面
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            cand = os.path.join(base or "", f"Inno Setup {ver}", "ISCC.exe")
            if os.path.isfile(cand):
                return cand
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found
    raise SystemExit("找不到 Inno Setup（ISCC.exe）：choco install innosetup 或从 jrsoftware.org 安装")


def package_windows(out: str, arch: str) -> list[str]:
    produced = []
    portable = os.path.join(out, artifact("windows", arch, "portable", ".zip"))
    zip_dir(BUNDLE, portable, "LANDrop", {PORTABLE_MARKER: PORTABLE_NOTE})
    produced.append(portable)
    base = artifact("windows", arch, "setup", "")
    run([find_iscc(), "/Qp", f"/DAppVersion={__version__}", f"/DSourceDir={BUNDLE}",
         f"/DOutputDir={os.path.abspath(out)}", f"/DOutputBase={base}",
         os.path.join(ROOT, "packaging", "windows", "landrop.iss")])
    produced.append(os.path.join(out, base + ".exe"))
    return produced


# --------------------------------------------------------------------------- macOS
def package_macos(out: str, arch: str) -> list[str]:
    app = os.path.join(DIST, "LAN Drop.app")
    if not os.path.isdir(app):
        raise SystemExit(f"找不到 {app}，请先在 macOS 上运行 pyinstaller landrop.spec")
    produced = []
    portable = os.path.join(out, artifact("macos", arch, "portable", ".zip"))
    # ditto 保留 .app 里的符号链接、权限与签名；zipfile 会把框架链接摊平
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, portable])
    produced.append(portable)

    work = os.path.join(DIST, "pkgwork")
    shutil.rmtree(work, ignore_errors=True)
    root = os.path.join(work, "root")
    os.makedirs(os.path.join(root, "Applications"))
    run(["ditto", app, os.path.join(root, "Applications", "LAN Drop.app")])
    comp = os.path.join(work, "component.plist")
    run(["pkgbuild", "--analyze", "--root", root, comp])
    with open(comp, "rb") as f:
        items = plistlib.load(f)
    for it in items:      # 不允许「重定位」：否则 Installer 会去升级磁盘上别处的同名 .app（比如构建目录）
        it["BundleIsRelocatable"] = False
        it["BundleHasStrictIdentifier"] = False
    with open(comp, "wb") as f:
        plistlib.dump(items, f)
    component_pkg = os.path.join(work, "landrop-component.pkg")
    run(["pkgbuild", "--root", root, "--component-plist", comp, "--install-location", "/",
         "--scripts", os.path.join(ROOT, "packaging", "macos", "scripts"),
         "--identifier", APP_ID, "--version", __version__, component_pkg])
    setup = os.path.join(out, artifact("macos", arch, "setup", ".pkg"))
    run(["productbuild", "--package", component_pkg, "--identifier", APP_ID,
         "--version", __version__, setup])
    produced.append(setup)
    shutil.rmtree(work, ignore_errors=True)
    return produced


# --------------------------------------------------------------------------- Linux
DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=LAN Drop
GenericName=LAN File Transfer
GenericName[zh_CN]=局域网文件互传
Comment=Send files across the LAN with a file code, or collect files from browsers
Comment[zh_CN]=用文件码在局域网内互传文件，或用浏览器收集文件
Exec={exec} %F
Icon=landrop
Terminal=false
Categories=Network;FileTransfer;Utility;
Keywords=share;send;receive;transfer;lan;
StartupWMClass=LAN Drop
"""


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info


def package_linux(out: str, arch: str) -> list[str]:
    produced = []
    portable = os.path.join(out, artifact("linux", arch, "portable", ".tar.gz"))
    note = os.path.join(DIST, PORTABLE_MARKER)
    with open(note, "w", encoding="utf-8") as f:
        f.write(PORTABLE_NOTE)
    with tarfile.open(portable, "w:gz") as tf:
        tf.add(BUNDLE, "LANDrop", filter=_tar_filter)
        tf.add(note, f"LANDrop/{PORTABLE_MARKER}", filter=_tar_filter)
    os.remove(note)
    produced.append(portable)

    deb_arch = {"x86_64": "amd64", "arm64": "arm64"}.get(arch, arch)
    stage = os.path.join(DIST, "debroot")
    shutil.rmtree(stage, ignore_errors=True)
    opt = os.path.join(stage, "opt", "landrop")
    shutil.copytree(BUNDLE, opt, symlinks=True)
    bindir = os.path.join(stage, "usr", "bin")
    os.makedirs(bindir)
    os.symlink("/opt/landrop/landrop", os.path.join(bindir, "landrop"))
    os.symlink("/opt/landrop/landrop-gui", os.path.join(bindir, "landrop-gui"))
    apps = os.path.join(stage, "usr", "share", "applications")
    os.makedirs(apps)
    with open(os.path.join(apps, "landrop.desktop"), "w", encoding="utf-8") as f:
        f.write(DESKTOP_ENTRY.format(exec="/opt/landrop/landrop-gui"))
    icons = os.path.join(stage, "usr", "share", "icons", "hicolor", "512x512", "apps")
    os.makedirs(icons)
    shutil.copy(os.path.join(ASSETS, "landrop.png"), os.path.join(icons, "landrop.png"))
    size_kb = sum(os.path.getsize(os.path.join(b, f)) for b, _, fs in os.walk(stage)
                  for f in fs if not os.path.islink(os.path.join(b, f))) // 1024
    debian = os.path.join(stage, "DEBIAN")
    os.makedirs(debian)
    with open(os.path.join(debian, "control"), "w", encoding="utf-8") as f:
        f.write(
            "Package: landrop\n"
            f"Version: {__version__}\n"
            "Section: net\n"
            "Priority: optional\n"
            f"Architecture: {deb_arch}\n"
            f"Installed-Size: {size_kb}\n"
            "Maintainer: LAN Drop contributors <noreply@github.com>\n"
            "Homepage: https://github.com/Introl-ljl/landrop\n"
            "Description: LAN file transfer with file codes, plus a self-hosted collection server\n"
            " Desktop window (landrop-gui) and command line (landrop) in one package:\n"
            " send files directly with a one-time file code, receive them with the code,\n"
            " or run a browser-based collection server for your LAN.\n")
    refresh = ("if command -v update-desktop-database >/dev/null 2>&1; then\n"
               "  update-desktop-database -q /usr/share/applications || true\nfi\n")
    icons = ("if command -v gtk-update-icon-cache >/dev/null 2>&1; then\n"
             "  gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true\nfi\n")
    for script, body in (("postinst", refresh + icons), ("postrm", refresh)):
        path = os.path.join(debian, script)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nset -e\n" + body)
        os.chmod(path, 0o755)
    for base, dirs, files in os.walk(stage):          # dpkg 要求规范权限
        for d in dirs:
            p = os.path.join(base, d)
            if not os.path.islink(p):
                os.chmod(p, 0o755)
        for fn in files:
            p = os.path.join(base, fn)
            if os.path.islink(p):
                continue
            mode = os.stat(p).st_mode
            os.chmod(p, 0o755 if mode & stat.S_IXUSR else 0o644)
    setup = os.path.join(out, artifact("linux", arch, "setup", ".deb"))
    run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", stage, setup])
    shutil.rmtree(stage, ignore_errors=True)
    produced.append(setup)
    return produced


def main(argv=None) -> int:
    setup_console()             # Windows 控制台默认 cp1252 / GBK：中文输出会抛 UnicodeEncodeError
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(DIST, "release"))
    ap.add_argument("--arch", default=detect_arch())
    args = ap.parse_args(argv)
    if not os.path.isdir(BUNDLE):
        raise SystemExit(f"找不到 {BUNDLE}，请先运行 pyinstaller --clean --noconfirm landrop.spec")
    os.makedirs(args.out, exist_ok=True)
    if sys.platform.startswith("win"):
        produced = package_windows(args.out, args.arch)
    elif sys.platform == "darwin":
        produced = package_macos(args.out, args.arch)
    else:
        produced = package_linux(args.out, args.arch)
    print("\n产物：")
    for p in produced:
        print(f"  {p}  ({os.path.getsize(p) / 1048576:.1f} MiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
