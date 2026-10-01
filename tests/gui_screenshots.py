#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给桌面窗口的每个页面截图（浅色 + 深色），CI 在三个平台上运行并作为 artifact 上传，便于检查各平台的实际观感。

    python tests/gui_screenshots.py <输出目录>        # 需要 Pillow（只在截图时用，不是运行依赖）
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "screenshots")
    os.makedirs(out, exist_ok=True)
    import subprocess
    for mode in ("light", "dark"):
        # 每个主题一个子进程：Tk 解释器与缓存都是全新的
        rc = subprocess.call([sys.executable, __file__, "--one", mode, out])
        if rc:
            return rc
    print("\n".join(sorted(os.listdir(out))))
    return 0


def one(mode, out):
    from PIL import ImageGrab
    tmp = tempfile.mkdtemp(prefix="landrop-shots-")
    os.environ.update(LANDROP_DATA_DIR=os.path.join(tmp, "data"), LANDROP_PORT="18765", LANDROP_SCOPE="local")
    os.environ.pop("LANDROP_URL", None)
    from landrop.gui import launcher, system
    system.save_settings({"theme": mode})
    app = launcher.LauncherApp()
    src = os.path.join(tmp, "src")
    os.makedirs(os.path.join(src, "旅行照片"))
    for name, size in (("项目方案-final-v3.pdf", 2_400_000), ("demo.mp4", 900_000), ("notes.txt", 1200)):
        with open(os.path.join(src, name), "wb") as f:
            f.write(os.urandom(size))
    for i in range(5):
        with open(os.path.join(src, "旅行照片", f"IMG_{i}.jpg"), "wb") as f:
            f.write(os.urandom(50000))

    def pump(t=0.6):
        end = time.time() + t
        while time.time() < end:
            app.root.update()
            time.sleep(0.02)

    def shot(name):
        app.root.lift()
        app.root.attributes("-topmost", True)
        pump(0.8)
        x, y = app.root.winfo_rootx(), app.root.winfo_rooty()
        w, h = app.root.winfo_width(), app.root.winfo_height()
        kw = {"xdisplay": os.environ["DISPLAY"]} if sys.platform.startswith("linux") else {}
        ImageGrab.grab(bbox=(x, y, x + w, y + h), **kw).save(os.path.join(out, f"{sys.platform}-{mode}-{name}.png"))

    app.root.geometry("+0+0")
    pump(1.2)
    shot("1-send")
    app.send.add_paths([os.path.join(src, n) for n in sorted(os.listdir(src))])
    shot("2-send-files")
    app.send.send()
    end = time.time() + 20
    while time.time() < end and not app.send.code.get():
        pump(0.2)
    shot("3-send-code")
    app.send.cancel()
    pump(0.8)
    app.select("receive")
    app.receive.code.set("192.168.1.5:41234/Xk3fQ2mZp9LwA7bC")
    shot("4-receive")
    app.select("service")
    app.start()
    pump(1.0)
    shot("5-service")
    app.stop()
    app.select("settings")
    shot("6-settings")
    app.on_close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--one":
        sys.exit(one(sys.argv[2], sys.argv[3]))
    sys.exit(main())
