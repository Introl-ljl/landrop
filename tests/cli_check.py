"""Real CLI processes. Reusable against source, zipapp and installed binaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
COMMAND = json.loads(os.environ["LANDROP_TEST_COMMAND"]) if os.environ.get("LANDROP_TEST_COMMAND") else [sys.executable, "-u", "-m", "landrop"]


class CLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.source = self.root / "source"
        (self.source / "folder" / "子目录").mkdir(parents=True)
        self.file = self.source / "big.bin"
        self.file.write_bytes(os.urandom(2 * 1024 * 1024))
        (self.source / "folder" / "子目录" / "中文.txt").write_text("Unicode payload", encoding="utf-8")
        self.process = None
        self.env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.run_cli("settings", "--port", str(self.port), "--ip", "127.0.0.1",
                     "--collect-dir", str(self.root / "collected"))

    def tearDown(self):
        if self.process:
            if self.process.poll() is None:
                self.run_cli("stop", check=False)
                try:
                    self.process.wait(15)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process.stdout.close()
        self.temp.cleanup()

    def run_cli(self, *args, input=None, check=True):
        result = subprocess.run([*COMMAND, "--data-dir", str(self.data), *args], cwd=str(ROOT), env=self.env,
                                input=input, capture_output=True, encoding="utf-8", timeout=60)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def spawn(self, *args, wait_for="访问地址:"):
        self.process = subprocess.Popen([*COMMAND, "--data-dir", str(self.data), *args], cwd=str(ROOT),
                                        env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        encoding="utf-8", bufsize=1)
        lines = queue.Queue()
        def pump():
            for line in self.process.stdout:
                lines.put(line)
        threading.Thread(target=pump, daemon=True).start()
        collected, end = "", time.monotonic() + 30
        while time.monotonic() < end:
            try:
                line = lines.get(timeout=0.2)
                collected += line
                if wait_for in line:
                    return collected
            except queue.Empty:
                if self.process.poll() is not None:
                    break
        self.fail("CLI did not start: " + collected)

    def shares(self):
        return json.loads(self.run_cli("shares", "--json").stdout)

    def test_send_receive_repeat_and_manual_stop(self):
        output = self.spawn("send", str(self.file), str(self.source / "folder"), "--upload", wait_for="授权码:")
        code = re.search(r"授权码:\s*([0-9]{6})", output).group(1)
        destination = self.root / "received"
        self.run_cli("get", self.base, "--code", code, "-o", str(destination))
        self.assertEqual((destination / "big.bin").read_bytes(), self.file.read_bytes())
        self.assertEqual((destination / "folder" / "子目录" / "中文.txt").read_text(encoding="utf-8"), "Unicode payload")
        self.assertIsNone(self.process.poll(), "Receiving must not stop the service")
        self.run_cli("get", self.base + "/#code=" + code, "-o", str(destination), "--only", "big.bin")
        self.assertEqual((destination / "big (1).bin").read_bytes(), self.file.read_bytes())
        self.run_cli("get", self.base, "--code", code, "-o", str(destination), "--only", "big.bin", "--force")
        self.assertFalse((destination / "big (2).bin").exists())
        self.run_cli("stop")
        self.assertEqual(self.process.wait(15), 0)
        self.assertTrue(self.file.exists())
        self.assertEqual(self.shares()[0]["code"], code)

    def test_local_manage_and_guest_collection(self):
        self.spawn("serve")
        self.run_cli("send", str(self.file), "--upload", "--public", "--text", "host text")
        first = self.shares()[0]
        self.run_cli("put", self.base, str(self.source / "folder"), "--code", first["code"], "--text", "guest text")
        listing = self.run_cli("get", self.base, "--code", first["code"], "--list").stdout
        self.assertIn("folder/子目录/中文.txt", listing)
        self.assertEqual(listing.count("文本 "), 2)
        directory = Path(first["collection_dir"])
        self.assertEqual((directory / "folder" / "子目录" / "中文.txt").read_text(encoding="utf-8"), "Unicode payload")
        self.run_cli("shares", first["id"], "--disable")
        self.assertNotEqual(self.run_cli("get", self.base, "--code", first["code"], "--list", check=False).returncode, 0)
        self.run_cli("shares", first["id"], "--enable")
        self.run_cli("shares", first["id"], "--rotate")
        rotated = self.shares()[0]
        self.assertNotEqual(rotated["code"], first["code"])
        self.assertNotEqual(self.run_cli("get", self.base, "--code", first["code"], "--list", check=False).returncode, 0)
        self.run_cli("get", self.base, "--code", rotated["code"], "--list")
        self.assertNotEqual(self.run_cli("shares", first["id"], "--delete", check=False).returncode, 0)
        self.run_cli("shares", first["id"], "--delete", "--yes")
        self.assertEqual(self.shares(), [])
        self.assertTrue(self.file.exists())
        self.assertTrue(directory.exists())

    def test_removed_commands_and_settings(self):
        for command in (("send", "--server", self.base), ("send", "--receivers", "1"),
                        ("serve", "--admin-key", "obsolete"), ("gui", "--autostart"),
                        ("get", self.base + "/obsolete-token")):
            self.assertNotEqual(self.run_cli(*command, check=False).returncode, 0)
        result = self.run_cli("settings", "--text-preview-mib", "16", "--image-preview-mib", "40")
        settings = json.loads(result.stdout)
        self.assertEqual(settings["text_preview_mib"], 16)
        self.assertEqual(settings["image_preview_mib"], 40)
        self.assertFalse((self.data / "admin-key.txt").exists())
        self.assertTrue((self.data / "shares.sqlite3").exists())

    def test_text_stdin_and_maximum(self):
        self.spawn("serve")
        self.run_cli("send", "--text", "-", input="a" * (10 * 1024 * 1024))
        share = self.shares()[0]
        listing = self.run_cli("get", self.base, "--code", share["code"], "--list").stdout
        self.assertIn("10.0 MiB", listing)
        previous = len(self.shares())
        self.assertNotEqual(self.run_cli("send", "--text", "-", input="a" * (10 * 1024 * 1024 + 1), check=False).returncode, 0)
        self.assertEqual(len(self.shares()), previous)

    def test_packaged_web_resources_and_no_management_page(self):
        self.spawn("serve")
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            self.assertIn(b"LAN Drop", response.read())
        with urllib.request.urlopen(self.base + "/logo.png", timeout=10) as response:
            self.assertTrue(response.read().startswith(b"\x89PNG\r\n\x1a\n"))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/admin", timeout=10)
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
