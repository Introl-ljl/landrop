# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。必须在目标操作系统上分别执行（不能交叉编译）::

    pyinstaller --clean --noconfirm landrop.spec
    python packaging/package.py          # 再把产物做成 安装版 + 免安装版

产物是**一个**应用目录，里面两个可执行文件共用同一份运行时：

    Windows   dist/LANDrop/LAN Drop.exe（窗口）  + landrop.exe（命令行）
    Linux     dist/LANDrop/landrop-gui（窗口）   + landrop（命令行）
    macOS     dist/LAN Drop.app/Contents/MacOS/LAN Drop（窗口）+ …/MacOS/landrop（命令行）

所以安装一次就同时拥有图形界面与 ``landrop`` 命令，不再有单独的命令行安装包。
"""

import os
import sys

ROOT = os.path.abspath(os.getcwd())
sys.path.insert(0, ROOT)
from landrop import __version__  # noqa: E402

STATIC = os.path.join(ROOT, "landrop", "server", "static")
ASSETS = os.path.join(ROOT, "packaging", "assets")
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# 网页静态资源；static/downloads/ 是 Docker 构建时放 pyz 的地方，本地可能残留旧二进制，不打进来
datas = []
for base, dirs, files in os.walk(STATIC):
    dirs[:] = [d for d in dirs if d != "downloads"]
    for fn in files:
        rel = os.path.relpath(base, ROOT)
        datas.append((os.path.join(base, fn), rel))

hidden = ["landrop.server.app", "landrop.server.store", "landrop.gui.launcher",
          "landrop.gui.panels", "landrop.gui.theme", "landrop.gui.widgets", "landrop.gui.icon",
          "landrop.gui.system"]
excludes = ["numpy", "PIL", "pytest", "setuptools", "pip"]
icon = os.path.join(ASSETS, "landrop.ico" if IS_WIN else "landrop.icns")

GUI_NAME = "LAN Drop" if (IS_WIN or IS_MAC) else "landrop-gui"
CLI_NAME = "landrop"


def analysis(entry):
    return Analysis([os.path.join("packaging", entry)], pathex=[ROOT], datas=datas,
                    hiddenimports=hidden, excludes=excludes, noarchive=False)


a_gui = analysis("entry_gui.py")
a_cli = analysis("entry_cli.py")

exe_gui = EXE(PYZ(a_gui.pure), a_gui.scripts, [], exclude_binaries=True, name=GUI_NAME,
              console=False, debug=False, strip=False, upx=False, icon=icon)
exe_cli = EXE(PYZ(a_cli.pure), a_cli.scripts, [], exclude_binaries=True, name=CLI_NAME,
              console=True, debug=False, strip=False, upx=False, icon=icon)

# 两个可执行文件进同一个目录；COLLECT 会按目标路径去重共用的库
coll = COLLECT(exe_gui, exe_cli, a_gui.binaries, a_gui.datas, a_cli.binaries, a_cli.datas,
               strip=False, upx=False, name="LANDrop")

if IS_MAC:
    # 第一个 EXE 是 .app 的主程序；命令行也在 Contents/MacOS/ 下，安装包会把它链接到 /usr/local/bin
    app = BUNDLE(coll, name="LAN Drop.app", icon=icon,
                 bundle_identifier="io.github.introl-ljl.landrop", version=__version__,
                 info_plist={
                     "CFBundleName": "LAN Drop",
                     "CFBundleDisplayName": "LAN Drop",
                     "CFBundleShortVersionString": __version__,
                     "CFBundleVersion": __version__,
                     "NSHighResolutionCapable": True,
                     "LSMinimumSystemVersion": "11.0",
                     "NSRequiresAquaSystemAppearance": False,
                     "LSApplicationCategoryType": "public.app-category.utilities",
                 })
