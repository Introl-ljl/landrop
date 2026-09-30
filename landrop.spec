# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。必须在目标操作系统上分别执行（不能跨平台交叉编译）::

    pyinstaller --clean --noconfirm landrop.spec

产物::

    dist/landrop        命令行（所有平台）：send / get / serve / revoke，含网页静态资源
    dist/landrop-gui    桌面窗口（仅 Windows 与 macOS 生成）

另有单文件 zipapp（任何装了 Python 3.8+ 的系统）::

    python packaging/build_pyz.py        # -> dist/landrop.pyz
"""

import os
import sys

ROOT = os.path.abspath(os.getcwd())
STATIC = os.path.join(ROOT, "landrop", "server", "static")
datas = [(STATIC, os.path.join("landrop", "server", "static"))]
IS_LINUX = sys.platform.startswith("linux")
common_excludes = ["numpy", "PIL", "pytest", "setuptools", "pip"]

# ---- 命令行（所有平台；不需要 tkinter） ----
a_cli = Analysis([os.path.join("packaging", "entry_cli.py")], pathex=[ROOT], datas=datas,
                 hiddenimports=["landrop.server.app", "landrop.server.store"],
                 excludes=common_excludes + ["tkinter"], noarchive=False)
pyz_cli = PYZ(a_cli.pure)
exe_cli = EXE(pyz_cli, a_cli.scripts, a_cli.binaries, a_cli.datas, [],
              name="landrop", debug=False, strip=False, upx=False, console=True)

# ---- 桌面窗口：仅 Windows 与 macOS ----
if not IS_LINUX:
    a_gui = Analysis([os.path.join("packaging", "entry_gui.py")], pathex=[ROOT], datas=datas,
                     hiddenimports=["landrop.server.app", "landrop.server.store",
                                    "landrop.gui.launcher", "landrop.gui.panels"],
                     excludes=common_excludes, noarchive=False)
    pyz_gui = PYZ(a_gui.pure)
    exe_gui = EXE(pyz_gui, a_gui.scripts, a_gui.binaries, a_gui.datas, [],
                  name="landrop-gui", debug=False, strip=False, upx=False, console=False)
