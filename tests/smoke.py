"""ADR contracts exercised through the real HTTP service and native client."""
from __future__ import annotations

import concurrent.futures
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from landrop.common import CliError, make_link, parse_target, sha256_file, stat_version
from landrop.remote import Client, download_file, list_remote, upload_file
from landrop.server.app import Runtime
from landrop.server.store import OverlapError, Store, TEXT_LIMIT


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.file = self.source / "hello.txt"
        self.file.write_bytes(b"hello world" * 100)
        self.data = self.root / "state"
        self.store = Store(str(self.data))
        self.store.configure(collect_dir=str(self.root / "collected"), recv_dir=str(self.root / "received"))
        self.runtime = Runtime(self.store)
        self.base = self.runtime.start(0, "127.0.0.1")["base"]

    def tearDown(self):
        self.runtime.close()
        self.store.close()
        self.temp.cleanup()

    def share(self, **values):
        return self.store.save_share(paths=[str(self.file)], **values)

    def client(self, share, name="receiver"):
        client = Client(self.base, str(self.root / name))
        client.login(share["code"])
        return client

    def error(self, status, action):
        with self.assertRaises(CliError) as caught:
            action()
        self.assertEqual(caught.exception.status, status, str(caught.exception))

    def upload(self, client, data, name="file.txt", upload_id=None, offset=0, total=None, digest=None):
        params = {"id": upload_id or os.urandom(16).hex(), "path": name, "offset": offset,
                  "total": len(data) if total is None else total,
                  "sha256": hashlib.sha256(data).hexdigest() if digest is None else digest}
        return client.request("PUT", "/api/upload?" + urlencode(params), data)

    def test_codes_leading_zero_and_retirement(self):
        with patch("landrop.server.store.secrets.randbelow", side_effect=[0, 0, 1]):
            share = self.share()
            self.assertEqual(share["code"], "000000")
            client = self.client(share)
            updated = self.store.rotate(share["id"])
            self.assertEqual(updated["code"], "000001")
        self.error(401, client.refresh)
        self.error(401, lambda: client.login("000000"))
        client.login("000001")
        self.store.delete_share(share["id"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM codes").fetchone()[0], 2)
        self.assertTrue(self.file.exists())

    def test_both_address_and_convenient_link(self):
        share = self.share()
        self.assertEqual(parse_target(make_link(self.base, share["code"])), (self.base, share["code"]))
        self.assertEqual(parse_target(f"访问地址: {self.base}\n授权码: {share['code']}"), (self.base, share["code"]))
        for old in (self.base + "/old-token", "192.168.1.5:8000/abcdefgh"):
            with self.assertRaises(CliError):
                parse_target(old)
        client, files = list_remote(make_link(self.base, share["code"]), data_dir=str(self.root / "native"))
        self.assertEqual(len(files), 1)
        self.assertEqual(client.content["share"]["id"], share["id"])

    def test_download_integrity_range_and_zero_file(self):
        empty = self.source / "empty.txt"
        empty.touch()
        share = self.store.save_share(paths=[str(self.source)])
        client = self.client(share)
        for item in client.content["files"]:
            out = download_file(client, item, str(self.root / "output"), on_progress=lambda *_: None)
            self.assertEqual(Path(out).read_bytes(), (self.source / Path(item["name"]).name).read_bytes())
        item = next(f for f in client.content["files"] if f["size"])
        with client.request("GET", "/api/download?" + urlencode({"id": item["id"]}),
                            headers={"Range": "bytes=3-8"}, raw=True) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), self.file.read_bytes()[3:9])
        with client.request("GET", "/api/download?" + urlencode({"id": item["id"]}),
                            headers={"Range": "bytes=3-8", "If-Range": '"old"'}, raw=True) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), self.file.read_bytes())

    def test_file_versions_match_windows_path_and_descriptor(self):
        fields = {"st_dev": 1, "st_ino": 2, "st_size": 3, "st_mtime_ns": 40, "st_birthtime_ns": 10}
        by_path = SimpleNamespace(**fields, st_ctime_ns=10)
        by_descriptor = SimpleNamespace(**fields, st_ctime_ns=20)
        with patch("landrop.common.os.name", "nt"):
            self.assertEqual(stat_version(by_path), stat_version(by_descriptor))
            changed = SimpleNamespace(**dict(fields, st_mtime_ns=41), st_ctime_ns=20)
            self.assertNotEqual(stat_version(by_path), stat_version(changed))
            legacy = SimpleNamespace(**{k: v for k, v in fields.items() if k != "st_birthtime_ns"}, st_ctime_ns=10)
            self.assertEqual(stat_version(by_path), stat_version(legacy))
        with patch("landrop.common.os.name", "posix"):
            self.assertNotEqual(stat_version(by_path), stat_version(by_descriptor))

    def test_scope_and_dynamic_directory(self):
        first = self.store.save_share(paths=[str(self.source)])
        other = self.root / "secret.txt"
        other.write_text("not shared", encoding="utf-8")
        second = self.store.save_share(paths=[str(other)])
        a, b = self.client(first, "a"), self.client(second, "b")
        forbidden = b.content["files"][0]["id"]
        self.error(404, lambda: a.request("GET", "/api/download?" + urlencode({"id": forbidden})))
        added = self.source / "new.txt"
        added.write_text("new", encoding="utf-8")
        self.assertEqual(len(a.refresh()["files"]), 2)
        added.unlink()
        self.assertEqual(len(a.refresh()["files"]), 1)
        if os.name != "nt":
            (self.source / "outside").symlink_to(other)
            self.assertEqual(len(a.refresh()["files"]), 1)

    def test_no_admin_no_delete_no_verify_routes(self):
        client = self.client(self.share())
        for path in ("/admin", "/api/grants", "/api/spaces", "/api/transfers", "/api/verify", "/api/delete", "/api/done"):
            for method in ("GET", "POST", "DELETE"):
                self.error(404, lambda p=path, m=method: client.request(m, p))
        self.assertFalse((self.data / "admin-key.txt").exists())
        self.assertEqual(self.runtime.info["base"], self.base)

    def test_disable_restore_expire_and_data_retention(self):
        share = self.share()
        self.store.add_text(share["id"], "keep me")
        client = self.client(share)
        self.store.set_enabled(share["id"], False)
        self.error(401, client.refresh)
        self.error(401, lambda: client.login(share["code"]))
        self.store.set_enabled(share["id"], True)
        client.login(share["code"])
        self.store.save_share(share["id"], expires_at=time.time() - 1)
        self.error(401, client.refresh)
        with self.assertRaises(CliError):
            self.store.set_enabled(share["id"], True)
        self.assertTrue(self.file.exists())
        self.assertEqual(self.store.texts(share["id"], local=True)[0]["size"], len("keep me"))
        self.store.save_share(share["id"], label="still expired")
        self.assertFalse(self.store.share(share["id"])["active"])
        self.store.save_share(share["id"], expires_at=time.time() + 3600)
        self.assertFalse(self.store.share(share["id"])["active"])
        self.store.set_enabled(share["id"], True)
        self.assertTrue(self.store.share(share["id"])["active"])

    def test_restart_preserves_code_text_and_no_automatic_start(self):
        share = self.share()
        text = self.store.add_text(share["id"], "persistent")
        self.runtime.stop()
        self.assertFalse(self.runtime.running)
        another = Store(str(self.data))
        try:
            self.assertEqual(another.share(share["id"])["code"], share["code"])
            self.assertEqual(another.text(share["id"], text, local=True), "persistent")
            self.assertIsNone(another.get_meta("service"))
        finally:
            another.close()
        self.base = self.runtime.start(0, "127.0.0.1")["base"]
        self.client(share)

    def test_upload_is_optional_private_and_visible_when_enabled(self):
        share = self.share()
        client = self.client(share)
        self.error(403, lambda: self.upload(client, b"blocked"))
        share = self.store.save_share(share["id"], allow_upload=True)
        directory = Path(share["collection_dir"])
        self.assertTrue(directory.is_dir())
        client.login(share["code"])
        self.upload(client, b"private")
        self.assertEqual(len(client.refresh()["files"]), 1)
        self.assertEqual(len(self.store.files(share["id"], local=True)), 2)
        self.store.save_share(share["id"], public_received=True)
        client.login(share["code"])
        self.assertEqual(len(client.refresh()["files"]), 2)
        self.store.delete_share(share["id"])
        self.assertEqual((directory / "file.txt").read_bytes(), b"private")

    def test_concurrent_same_name_uploads_and_no_overwrite(self):
        share = self.share(allow_upload=True, public_received=True)
        clients = [self.client(share, f"sender{i}") for i in range(6)]
        payloads = [f"payload {i}".encode() for i in range(6)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda pair: self.upload(pair[0], pair[1]), zip(clients, payloads)))
        self.assertEqual(len({r["name"] for r in results}), 6)
        files = list(Path(share["collection_dir"]).iterdir())
        self.assertEqual({f.read_bytes() for f in files}, set(payloads))

    def test_upload_resume_idempotency_ownership_and_mismatch(self):
        share = self.share(allow_upload=True, public_received=True)
        a, b = self.client(share, "one"), self.client(share, "two")
        upload_id = "1234567890abcdef"
        data = b"a" * 20
        digest = hashlib.sha256(data).hexdigest()
        first = self.upload(a, data[:10], upload_id=upload_id, total=20, digest=digest)
        self.assertFalse(first["complete"])
        self.error(400, lambda: self.upload(b, data[10:], upload_id=upload_id, total=20, offset=10, digest=digest))
        self.error(409, lambda: self.upload(a, data[:10], upload_id=upload_id, total=20, digest=digest))
        result = self.upload(a, data[10:], upload_id=upload_id, total=20, offset=10, digest=digest)
        repeat = self.upload(a, data[10:], upload_id=upload_id, total=20, offset=10, digest=digest)
        self.assertEqual(result, repeat)
        self.assertEqual(len(list(Path(share["collection_dir"]).iterdir())), 1)
        self.error(422, lambda: self.upload(a, b"bad", name="bad.txt", digest="0" * 64))
        self.assertFalse((Path(share["collection_dir"]) / "bad.txt").exists())
        self.assertFalse(any(f["name"] == "bad.txt" for f in a.refresh()["files"]))

    def test_directory_change_keeps_old_files_and_share_edit_keeps_text(self):
        share = self.share(allow_upload=True, public_received=True)
        client = self.client(share)
        self.upload(client, b"old")
        text = self.store.add_text(share["id"], "keep")
        updated = self.store.save_share(share["id"], collection_base=str(self.root / "new-base"))
        self.assertNotEqual(updated["collection_dir"], share["collection_dir"])
        client.login(share["code"])
        self.upload(client, b"new")
        self.assertEqual(len(client.refresh()["files"]), 3)
        self.assertEqual(self.store.text(share["id"], text, True), "keep")
        self.assertEqual((Path(share["collection_dir"]) / "file.txt").read_bytes(), b"old")

        self.store.save_share(share["id"], allow_upload=False)
        moved = self.store.save_share(share["id"], collection_base=str(self.root / "paused-base"))
        self.assertNotEqual(moved["collection_dir"], updated["collection_dir"])
        self.store.save_share(share["id"], allow_upload=True)
        client.login(share["code"])
        self.upload(client, b"after re-enable")
        self.assertEqual((Path(moved["collection_dir"]) / "file.txt").read_bytes(), b"after re-enable")
        self.assertEqual((Path(updated["collection_dir"]) / "file.txt").read_bytes(), b"new")
        self.assertEqual(len(client.refresh()["files"]), 4)

    def test_overlap_requires_explicit_local_confirmation(self):
        collector = self.store.save_share(paths=[], allow_upload=True)
        with self.assertRaises(OverlapError):
            self.store.save_share(paths=[collector["collection_base"]])
        directory = self.store.save_share(paths=[collector["collection_base"]], confirm_overlap=True)
        visitor = self.client(collector)
        self.upload(visitor, b"private for collection code")
        self.assertEqual(visitor.refresh()["files"], [])
        reader = self.client(directory, "directory-reader")
        self.assertEqual(len(reader.content["files"]), 1)

    def test_text_permissions_limit_and_clear(self):
        share = self.share(allow_upload=True)
        published = self.store.add_text(share["id"], "from host")
        client = self.client(share)
        received = client.submit_text("private reply")["id"]
        self.assertEqual(client.text(published), "from host")
        self.error(404, lambda: client.text(received))
        self.assertEqual(len(client.refresh()["texts"]), 1)
        self.store.save_share(share["id"], public_received=True)
        client.login(share["code"])
        self.assertEqual(client.text(received), "private reply")
        self.store.add_text(share["id"], "a" * TEXT_LIMIT)
        with self.assertRaises(CliError):
            self.store.add_text(share["id"], "a" * (TEXT_LIMIT + 1))
        for index in range(97):
            self.store.add_text(share["id"], str(index))
        with self.assertRaises(CliError):
            self.store.add_text(share["id"], "overflow")
        self.assertEqual(len(self.store.texts(share["id"], True)), 100)
        self.store.clear_texts(share["id"])
        self.assertEqual(self.store.texts(share["id"], True), [])
        self.assertTrue(self.file.exists())

    def test_preview_limits_can_change_and_full_download_still_allowed(self):
        self.file.write_bytes(b"x" * (2 * 1024 * 1024))
        share = self.share()
        client = self.client(share)
        self.store.configure(text_preview_mib=1)
        item = client.content["files"][0]
        params = {"id": item["id"], "preview": "text"}
        self.error(413, lambda: client.request("GET", "/api/download?" + urlencode(params), raw=True))
        self.store.configure(text_preview_mib=10)
        with client.request("GET", "/api/download?" + urlencode(params), raw=True) as response:
            self.assertEqual(len(response.read()), item["size"])
        self.assertEqual(client.refresh()["limits"]["text_preview"], TEXT_LIMIT)

    def test_csrf_login_throttle_does_not_stop_service(self):
        share = self.share(allow_upload=True)
        valid = self.client(share)
        self.error(403, lambda: valid.request("POST", "/api/text", b"bad", {"Origin": "http://evil.example"}))
        stranger = Client(self.base, str(self.root / "stranger"))
        for _ in range(10):
            self.error(401, lambda: stranger.login("not-a-code"))
        self.error(429, lambda: stranger.login(share["code"]))
        self.assertEqual(len(valid.refresh()["files"]), 1)
        self.assertTrue(self.runtime.running)

    def test_only_one_service_and_local_control_has_no_network_key(self):
        other_store = Store(str(self.data))
        other = Runtime(other_store)
        try:
            info = other.start(0, "127.0.0.1")
            self.assertEqual(info["base"], self.base)
            self.assertIsNone(other.httpd)
            other.stop()
            self.assertFalse(self.runtime.running)
        finally:
            other.close()
            other_store.close()

    def test_cancel_cleanup_and_retention(self):
        share = self.share(allow_upload=True, public_received=True)
        client = self.client(share)
        upload_id = "abcdef1234567890"
        self.upload(client, b"a", upload_id=upload_id, total=2, digest="")
        client.request("DELETE", "/api/upload?" + urlencode({"id": upload_id}))
        self.assertFalse(Path(self.store._partial(upload_id)).exists())
        self.upload(client, b"a", upload_id=upload_id, total=2, digest="")
        with self.store.transaction() as db:
            db.execute("UPDATE uploads SET updated_at=? WHERE id=?", (time.time() - 86401, upload_id))
        self.store.cleanup()
        self.assertFalse(Path(self.store._partial(upload_id)).exists())
        self.upload(client, b"complete")
        self.store.cleanup()
        self.assertEqual((Path(share["collection_dir"]) / "file.txt").read_bytes(), b"complete")

    def test_private_metadata_is_never_a_directory_share(self):
        share = self.store.save_share(paths=[str(self.root)], confirm_overlap=True)
        client = self.client(share)
        names = [item["name"] for item in client.content["files"]]
        self.assertFalse(any("shares.sqlite3" in name or "diagnostics.log" in name or "service.lock" in name for name in names))

    def test_upload_paths_and_symlinks_cannot_escape_collection(self):
        share = self.share(allow_upload=True, public_received=True)
        client = self.client(share)
        for name in ("../escape.txt", "/tmp/escape.txt", "C:\\escape.txt"):
            self.error(400, lambda value=name: self.upload(client, b"bad", name=value))
        self.assertFalse((self.root / "escape.txt").exists())
        if os.name != "nt":
            outside = self.root / "outside"
            outside.mkdir()
            (Path(share["collection_dir"]) / "escape").symlink_to(outside, target_is_directory=True)
            self.error(400, lambda: self.upload(client, b"bad", name="escape/leak.txt"))
            self.assertFalse((outside / "leak.txt").exists())

    def test_local_editor_conflicts_and_atomic_text_creation(self):
        share = self.share()
        self.store.rotate(share["id"])
        with self.assertRaises(CliError):
            self.store.save_share(share["id"], label="stale", expected_version=share["version"])
        count = len(self.store.shares())
        with self.assertRaises(CliError):
            self.store.save_share(paths=[], allow_upload=True, text="x" * (TEXT_LIMIT + 1))
        self.assertEqual(len(self.store.shares()), count)

    def test_zip_selected_files_and_invalid_scope(self):
        share = self.store.save_share(paths=[str(self.source)])
        client = self.client(share)
        ids = [item["id"] for item in client.content["files"]]
        with client.request("POST", "/api/zip", {"ids": ids}, raw=True) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
            self.assertEqual(archive.read(archive.namelist()[0]), self.file.read_bytes())
        self.error(403, lambda: client.request("POST", "/api/zip", {"ids": ["foreign"]}))
        self.error(403, lambda: client.request("POST", "/api/zip", {"ids": [{}]}))
        form = urlencode({"ids": json.dumps(ids)}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": self.base}
        with client.request("POST", "/api/zip", form, headers, raw=True) as response:
            self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
            self.assertEqual(archive.read(archive.namelist()[0]), self.file.read_bytes())
        self.error(403, lambda: client.request("POST", "/api/zip", form, dict(headers, Origin="null")))
        self.error(403, lambda: client.request("POST", "/api/zip", form, dict(headers, Origin="http://evil.example")))

    def test_recent_history_and_legacy_backup(self):
        for index in range(7):
            self.store.record_receive(index, str(self.root / str(index)))
        history = self.store.settings()["receive_history"]
        self.assertEqual([row["count"] for row in history], [6, 5, 4, 3, 2])
        self.assertEqual(set(history[0]), {"count", "directory", "time"})
        old = self.root / "legacy"
        old.mkdir()
        with closing(sqlite3.connect(str(old / "state.sqlite3"))) as db:
            db.execute("CREATE TABLE grants(secret TEXT)")
            db.execute("INSERT INTO grants VALUES('old-credential')")
            db.commit()
        (old / "admin-key.txt").write_text("old-credential", encoding="utf-8")
        (old / "desktop.json").write_text(json.dumps({"theme": "dark", "port": "8999"}), encoding="utf-8")
        preserved = old / "files"
        preserved.mkdir()
        (preserved / "old.txt").write_text("keep", encoding="utf-8")
        migrated = Store(str(old))
        try:
            self.assertEqual(migrated.shares(), [])
            self.assertEqual(migrated.settings()["port"], 8999)
            self.assertTrue(list((old / "legacy-backup").glob("*/state.sqlite3")))
            self.assertEqual((preserved / "old.txt").read_text(), "keep")
            migrated.close()
            migrated = Store(str(old))
            self.assertEqual(len(list((old / "legacy-backup").iterdir())), 1)
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
