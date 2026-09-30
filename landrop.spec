# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把服务端、桌面启动器与静态页面打成一个分发包。

用法（必须在目标操作系统上分别执行，不能跨平台交叉编译）::

    pyinstaller --clean --noconfirm landrop.spec

产物::

    dist/landrop-cli        命令行：send 生成文件码 / get 取件 / revoke 撤销 / serve 启动服务
    dist/landrop-server     纯命令行服务器（保留控制台输出）
    dist/landrop            桌面启动器（GUI：服务 / 发送 / 接收），仅 Windows 与 macOS 生成

平台分工：Windows / macOS 面向 GUI 用户；Linux 只出命令行产物（无 tkinter 依赖）。

说明:
  * 静态页面通过 datas 一并打包，server.resource_dir() 在运行时定位解包目录。
  * 数据目录默认放在用户目录（见 desktop.default_data_dir），不会写进安装目录。
  * 真正的原生窗口需要 pywebview/Tauri 之类额外运行时；这里刻意不引入。
"""

import os
import sys

ROOT = os.path.abspath(os.getcwd())
datas = [(os.path.join(ROOT, "static"), "static")]
common = dict(
    pathex=[ROOT],
    hiddenimports=["store", "server", "desktop", "desktop_share", "landrop"],
    excludes=["numpy", "PIL", "pytest", "setuptools", "pip"],
    noarchive=False,
)

IS_LINUX = sys.platform.startswith("linux")
server_excludes = common["excludes"] + ["tkinter"]

# ---- 命令行（所有平台）：send / get / revoke / serve ----
a_cli = Analysis(["landrop.py"], datas=datas, pathex=[ROOT],
                 hiddenimports=["store", "server"], excludes=server_excludes, noarchive=False)
pyz_cli = PYZ(a_cli.pure)
exe_cli = EXE(pyz_cli, a_cli.scripts, a_cli.binaries, a_cli.datas, [],
              name="landrop-cli", debug=False, strip=False, upx=False, console=True)

# ---- 纯命令行服务器（所有平台，参数同 server.py） ----
a_srv = Analysis(["server.py"], datas=datas, pathex=[ROOT],
                 hiddenimports=["store", "server"], excludes=server_excludes, noarchive=False)
pyz_srv = PYZ(a_srv.pure)
exe_srv = EXE(pyz_srv, a_srv.scripts, a_srv.binaries, a_srv.datas, [],
              name="landrop-server", debug=False, strip=False, upx=False, console=True)

# ---- 桌面启动器（GUI：服务 / 发送 / 接收）：仅 Windows 与 macOS ----
if not IS_LINUX:
    a_gui = Analysis(["desktop.py"], datas=datas, **common)
    pyz_gui = PYZ(a_gui.pure)
    exe_gui = EXE(pyz_gui, a_gui.scripts, a_gui.binaries, a_gui.datas, [],
                  name="landrop", debug=False, strip=False, upx=False, console=False)
