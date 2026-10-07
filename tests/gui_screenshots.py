"""Capture actual three-page workflows in light and dark mode (Pillow is test-only)."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from PIL import ImageGrab
    from landrop.gui.launcher import LauncherApp
    from landrop.server.store import Store
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "screenshots").resolve()
    out.mkdir(parents=True, exist_ok=True)
    for mode in ("light", "dark"):
        with tempfile.TemporaryDirectory(prefix="landrop-shots-") as temporary:
            root = Path(temporary)
            data = root / "state"
            os.environ.update(LANDROP_DATA_DIR=str(data), LANDROP_NO_DND="1")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            store = Store(str(data))
            store.configure(theme=mode, ip="127.0.0.1", port=port,
                            collect_dir=str(root / "collected"), recv_dir=str(root / "received"))
            store.close()
            source = root / "资料"
            source.mkdir()
            (source / "项目资料.txt").write_text("Native desktop verification", encoding="utf-8")
            app = LauncherApp()
            def pump(duration=0.3):
                end = time.monotonic() + duration
                while time.monotonic() < end:
                    app.root.update()
                    time.sleep(0.01)
            def wait(page):
                end = time.monotonic() + 30
                while page.busy and time.monotonic() < end:
                    pump(0.05)
                if page.busy:
                    raise RuntimeError("Screenshot workflow did not finish")
            def shot(name):
                app.root.lift()
                app.root.attributes("-topmost", True)
                pump()
                x, y = app.root.winfo_rootx(), app.root.winfo_rooty()
                width, height = app.root.winfo_width(), app.root.winfo_height()
                options = {"xdisplay": os.environ["DISPLAY"]} if sys.platform.startswith("linux") else {}
                ImageGrab.grab(bbox=(x, y, x + width, y + height), **options).save(out / f"{sys.platform}-{mode}-{name}.png")
            try:
                app.root.geometry("1040x760+0+0")
                shot("share-empty")
                app.share.add_paths([str(source)])
                app.share.label.set("资料分享与文件收集")
                app.share.upload.set(True)
                app.share.public.set(True)
                app.share.text.insert("1.0", "这是本机发布的文本")
                app.share.save_button.invoke()
                wait(app.share)
                shot("share-active")
                share = app.share.item
                app.select("receive")
                app.receive.address.set(app.runtime.info["base"])
                app.receive.code.set(share["code"])
                app.receive.connect_button.invoke()
                wait(app.receive)
                shot("receive")
                app.receive.receive_button.invoke()
                wait(app.receive)
                shot("receive-complete")
                app.select("settings")
                shot("settings")
            finally:
                app.receive.shutdown()
                app.runtime.close()
                app.share.shutdown()
                app.toast.hide()
                app.bridge.close()
                app.store.close()
                app.root.destroy()
    print("Screenshots:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
