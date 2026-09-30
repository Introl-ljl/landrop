#!/usr/bin/env python3
"""把 landrop 包打成单文件 zipapp（landrop.pyz）：有 Python 3.8+ 的机器拷过去就能运行。

    python packaging/build_pyz.py [输出路径]      # 默认 dist/landrop.pyz
    python landrop.pyz send a.zip
"""
import os
import shutil
import sys
import tempfile
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv):
    out = os.path.abspath(argv[1] if len(argv) > 1 else os.path.join(ROOT, "dist", "landrop.pyz"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    stage = tempfile.mkdtemp(prefix="landrop-pyz-")
    try:
        shutil.copytree(os.path.join(ROOT, "landrop"), os.path.join(stage, "landrop"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "downloads"))
        with open(os.path.join(stage, "__main__.py"), "w", encoding="utf-8") as f:
            f.write("import sys\nfrom landrop.cli import main\nsys.exit(main())\n")
        zipapp.create_archive(stage, out, interpreter="/usr/bin/env python3", compressed=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
