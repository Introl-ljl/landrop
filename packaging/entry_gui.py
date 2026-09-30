"""PyInstaller 入口：桌面窗口（Windows / macOS）。"""
import sys

from landrop.gui.launcher import main

if __name__ == "__main__":
    sys.exit(main())
