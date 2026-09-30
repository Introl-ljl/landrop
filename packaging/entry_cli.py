"""PyInstaller 入口：命令行（含 serve）。"""
import sys

from landrop.cli import main

if __name__ == "__main__":
    sys.exit(main())
