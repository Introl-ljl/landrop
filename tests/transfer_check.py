"""Fault injection against real streams: interruption, versions and permission changes."""
from __future__ import annotations

import concurrent.futures
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from landrop.common import CliError
from landrop.remote import Client, download_file
from landrop.server import app
from landrop.server.store import Store


class FaultWriter:
    def __init__(self, writer, handler):
        self.writer, self.handler, self.headers = writer, handler, True

    def __getattr__(self, key):
        return getattr(self.writer, key)

    def write(self, data):
        if self.headers:
            self.headers = False
            return self.writer.write(data)
        runtime = self.handler.runtime
        if runtime.fault == "drop":
            self.writer.write(data[:65536])
            self.handler.connection.shutdown(socket.SHUT_RDWR)
            raise BrokenPipeError("injected interruption")
        result = self.writer.write(data)
        runtime.callback()
        self.handler.wfile = self.writer
        return result


class FaultHandler(app.Handler):
    def _download(self, auth, params):
        self.runtime.ranges.append(self.headers.get("Range", ""))
        if self.runtime.fault and not self.runtime.failed:
            self.runtime.failed = True
            self.wfile = FaultWriter(self.wfile, self)
        return super()._download(auth, params)


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / "payload.bin"
        self.file.write_bytes(b"A" * (2 * 1024 * 1024))
        self.store = Store(str(self.root / "state"))
        self.store.configure(collect_dir=str(self.root / "collected"))
        self.share = self.store.save_share(paths=[str(self.file)], allow_upload=True, public_received=True)
        self.runtime = app.Runtime(self.store)
        self.runtime.fault, self.runtime.failed, self.runtime.ranges = "", False, []
        self.runtime.callback = lambda: None
        with patch("landrop.server.app.Handler", FaultHandler):
            self.base = self.runtime.start(0, "127.0.0.1")["base"]
        self.client = Client(self.base, str(self.root / "receiver"))
        self.client.login(self.share["code"])

    def tearDown(self):
        self.runtime.close()
        self.store.close()
        self.temp.cleanup()

    def receive(self):
        return download_file(self.client, self.client.content["files"][0], str(self.root / "output"),
                             on_progress=lambda *_: None)

    def test_interruption_resumes_same_version(self):
        self.runtime.fault = "drop"
        with self.assertRaises(CliError):
            self.receive()
        part = next((self.root / "output").glob(".landrop-*.part"))
        self.assertEqual(part.stat().st_size, 65536)
        target = self.receive()
        self.assertEqual(Path(target).read_bytes(), self.file.read_bytes())
        self.assertIn("bytes=65536-", self.runtime.ranges)
        self.assertFalse(part.exists())

    def test_changed_version_does_not_mix_partial_data(self):
        self.runtime.fault = "drop"
        with self.assertRaises(CliError):
            self.receive()
        self.file.write_bytes(b"B" * (2 * 1024 * 1024))
        self.client.refresh()
        target = self.receive()
        self.assertEqual(Path(target).read_bytes(), b"B" * (2 * 1024 * 1024))
        self.assertEqual(self.runtime.ranges[-1], "")

    def test_source_changes_during_stream_are_not_published(self):
        self.runtime.fault = "mutate"
        self.runtime.callback = lambda: self.file.write_bytes(b"B" * (2 * 1024 * 1024))
        with self.assertRaises(CliError):
            self.receive()
        self.assertFalse((self.root / "output" / self.file.name).exists())
        self.client.refresh()
        target = self.receive()
        self.assertEqual(Path(target).read_bytes(), self.file.read_bytes())

    def test_rotate_stops_stream_and_reauthorizes_resume(self):
        self.runtime.fault = "rotate"
        codes = []
        self.runtime.callback = lambda: codes.append(self.store.rotate(self.share["id"])["code"])
        with self.assertRaises(CliError):
            self.receive()
        self.client.login(codes[0])
        target = self.receive()
        self.assertEqual(Path(target).read_bytes(), self.file.read_bytes())
        self.assertTrue(self.runtime.ranges[-1].startswith("bytes="))

    def test_disable_while_finalizing_upload_interrupts_without_publishing(self):
        started, resume = threading.Event(), threading.Event()
        original = self.store._finish_upload
        def finalize(auth, upload_id, guard):
            def paused_guard():
                started.set()
                if not resume.wait(10):
                    raise RuntimeError("test timeout")
                guard()
            return original(auth, upload_id, paused_guard)
        with patch.object(self.store, "_finish_upload", finalize), concurrent.futures.ThreadPoolExecutor() as pool:
            query = urlencode({"id": "1234567890abcdef", "name": "blocked.bin", "total": 2 * 1024 * 1024, "offset": 0})
            future = pool.submit(self.client.request, "PUT", "/api/upload?" + query, b"A" * (2 * 1024 * 1024))
            self.assertTrue(started.wait(10))
            self.store.set_enabled(self.share["id"], False)
            resume.set()
            with self.assertRaises(CliError):
                future.result(10)
        self.assertFalse(list(Path(self.share["collection_dir"]).glob("*.bin")))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM uploads WHERE complete=1").fetchone()[0], 0)

    def test_explicit_cancel_during_publication_removes_only_temporary_data(self):
        started, resume = threading.Event(), threading.Event()
        original = self.store._finish_upload
        def finalize(auth, upload_id, guard):
            def paused_guard():
                started.set()
                if not resume.wait(10):
                    raise RuntimeError("test timeout")
                guard()
            return original(auth, upload_id, paused_guard)
        upload_id = "fedcba0987654321"
        with patch.object(self.store, "_finish_upload", finalize), concurrent.futures.ThreadPoolExecutor() as pool:
            query = urlencode({"id": upload_id, "name": "cancelled.bin", "total": 2 * 1024 * 1024, "offset": 0})
            future = pool.submit(self.client.request, "PUT", "/api/upload?" + query, b"A" * (2 * 1024 * 1024))
            self.assertTrue(started.wait(10))
            self.client.request("DELETE", "/api/upload?" + urlencode({"id": upload_id}))
            resume.set()
            with self.assertRaises(CliError):
                future.result(10)
        self.assertFalse(Path(self.store._partial(upload_id)).exists())
        self.assertFalse(list(Path(self.share["collection_dir"]).glob("*.bin")))
        self.assertTrue(self.file.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
