"""Drive real desktop controls through sharing, receiving, collection and settings."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from landrop.gui.launcher import LauncherApp
from landrop.remote import Client


class Desktop(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"LANDROP_DATA_DIR": str(self.root / "data"), "LANDROP_NO_DND": "1"})
        self.environment.start()
        self.confirm = patch("tkinter.messagebox.askokcancel", return_value=True)
        self.confirm.start()
        self.overlap = patch("tkinter.messagebox.askyesno", return_value=True)
        self.overlap.start()
        self.app = LauncherApp()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.app.store.configure(ip="127.0.0.1", port=port, collect_dir=str(self.root / "collected"),
                                 recv_dir=str(self.root / "received"))
        self.source = self.root / "source"
        self.source.mkdir()
        self.file = self.source / "测试 文件.txt"
        self.file.write_text("desktop payload", encoding="utf-8")
        self.pump()

    def tearDown(self):
        self.app.receive.shutdown()
        self.app.runtime.close()
        self.app.share.shutdown()
        self.app.toast.hide()
        self.app.bridge.close()
        self.app.store.close()
        self.app.root.destroy()
        self.overlap.stop()
        self.confirm.stop()
        self.environment.stop()
        self.temp.cleanup()

    def pump(self, duration=0.1):
        end = time.monotonic() + duration
        while time.monotonic() < end:
            self.app.root.update()
            time.sleep(0.01)

    def wait(self, predicate, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.04)
            if predicate():
                return
        self.fail("Desktop operation did not finish")

    def create(self):
        page = self.app.share
        page.new()
        page.add_paths([str(self.source)])
        page.collection.set(str(self.root / "collected"))
        page.upload.set(True)
        page.text.insert("1.0", "published text")
        page.save_button.invoke()
        self.wait(lambda: not page.busy)
        self.assertIsNotNone(page.item)
        self.assertTrue(self.app.runtime.running)
        return page.item

    def test_three_pages_no_autostart_and_end_to_end(self):
        self.assertEqual(set(self.app.pages), {"share", "receive", "settings"})
        self.assertFalse(self.app.runtime.running)
        share = self.create()
        self.assertEqual(len(self.app.share.code.get()), 6)
        self.app.share.copy_info()
        self.assertIn(share["code"], self.app.root.clipboard_get())
        self.app.select("receive")
        page = self.app.receive
        page.address.set(self.app.runtime.info["base"])
        page.code.set(share["code"])
        page.connect_button.invoke()
        self.wait(lambda: not page.busy)
        self.assertEqual(len(page.files), 1)
        self.assertEqual(len(page.client.content["texts"]), 1)
        page.receive_button.invoke()
        self.wait(lambda: not page.busy)
        received = self.root / "received" / "source" / self.file.name
        self.assertEqual(received.read_text(encoding="utf-8"), "desktop payload")
        self.assertEqual(self.app.store.settings()["receive_history"][0]["count"], 1)
        page.submit_text.insert("1.0", "private guest text")
        page.submit_button.invoke()
        self.wait(lambda: not page.busy)
        self.assertEqual(len(self.app.store.texts(share["id"], local=True)), 2)
        self.assertEqual(len(page.client.refresh()["texts"]), 1)
        self.app.select("share")
        self.app.share.public.set(True)
        self.app.share.save_button.invoke()
        self.wait(lambda: not self.app.share.busy)
        client = Client(self.app.runtime.info["base"], str(self.root / "browser-equivalent"))
        client.login(share["code"])
        self.assertEqual(len(client.content["texts"]), 2)
        self.app.share.rotate()
        self.wait(lambda: not self.app.share.busy)
        self.assertNotEqual(self.app.share.item["code"], share["code"])
        self.assertTrue(self.file.exists())

    def test_settings_history_and_theme(self):
        self.create()
        self.app.select("settings")
        page = self.app.settings_page
        page.variables["text_preview_mib"].set("15")
        page.variables["image_preview_mib"].set("40")
        page.save_button.invoke()
        self.wait(lambda: not page.busy)
        self.assertEqual(self.app.store.settings()["text_preview_mib"], 15)
        self.assertEqual(self.app.store.settings()["image_preview_mib"], 40)
        for theme in ("dark", "light", "system"):
            self.app.apply_theme(theme)
            self.pump()
            self.assertEqual(self.app.store.settings()["theme"], theme)
        self.app.store.record_receive(1, str(self.root / "received"))
        self.app.select("receive")
        self.assertTrue(self.app.receive.history_rows.winfo_children())
        self.app.receive.clear_history()
        self.assertEqual(self.app.store.settings()["receive_history"], [])

    def test_disabled_expired_and_delete_are_local_only(self):
        share = self.create()
        page = self.app.share
        page.enabled.set(False)
        page.toggle_enabled()
        self.wait(lambda: not page.busy)
        self.assertFalse(self.app.store.share(share["id"])["active"])
        self.assertEqual(self.app.store.share(share["id"])["code"], share["code"])
        page.enabled.set(True)
        page.toggle_enabled()
        self.wait(lambda: not page.busy)
        self.assertTrue(self.app.store.share(share["id"])["active"])
        directory = Path(page.item["collection_dir"])
        page.delete()
        self.wait(lambda: not page.busy)
        self.assertEqual(self.app.store.shares(), [])
        self.assertTrue(directory.exists())
        self.assertTrue(self.file.exists())

    def test_close_with_active_task_requires_confirmation(self):
        self.app.set_busy("receive", True)
        with patch("tkinter.messagebox.askokcancel", return_value=False) as confirm:
            self.app.on_close()
            confirm.assert_called_once()
        self.assertTrue(self.app.root.winfo_exists())
        self.app.set_busy("receive", False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
