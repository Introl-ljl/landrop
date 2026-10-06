"""Regression tests for pickup coverage and upload recovery, using real HTTP."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from landrop import common, direct, remote
from landrop.server import app
from landrop.server.store import Store


class FailingWriter:
    def __init__(self, handler):
        self.handler = handler
        self.original = handler.wfile
        self.writes = 0

    def __getattr__(self, name):
        return getattr(self.original, name)

    def write(self, data):
        self.writes += 1
        if self.writes == 2:
            self.handler.close_connection = True
            self.handler.connection.shutdown(socket.SHUT_RDWR)
            raise ConnectionResetError("injected download reset")
        return self.original.write(data)


class FaultHandler(app.Handler):
    def setup(self):
        self.store = app.STORE
        super().setup()

    def finish(self):
        try:
            super().finish()
        finally:
            # SQLite connections must be closed on their owning request threads.
            self.store.close()

    def do_PUT(self):
        self.server.puts += 1
        super().do_PUT()

    def do_GET(self):
        if self.path.startswith('/api/download') and 'progress=1' not in self.path:
            self.server.downloads.append(self.headers.get('Range'))
        super().do_GET()

    def end_headers(self):
        super().end_headers()
        if self.path.startswith('/api/download') and self.server.fault == 'download':
            self.server.fault = ''
            self.wfile = FailingWriter(self)

    def _send_json(self, obj, status=200, cookies=None):
        fault = self.server.fault
        if (self.command == 'PUT' and obj.get('ok') and
                (obj.get('complete') or fault == 'upload_partial') and
                fault.startswith('upload_')):
            self.server.fault = ''
            if fault == 'upload_body':
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body[:len(body) // 2])
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        super()._send_json(obj, status, cookies)


class Transfers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='landrop-regression-')
        self.store = Store(os.path.join(self.tmp.name, 'data'))
        self.default = self.store.create_space('default')
        self.store.init_admin('test-key')
        self.patches = [patch.object(app, 'STORE', self.store),
                        patch.object(app, 'DEFAULT_SPACE', self.default),
                        patch.object(app, 'log', lambda msg: None),
                        patch.object(remote, 'default_data_dir', lambda: self.tmp.name)]
        for p in self.patches:
            p.start()
        self.server = app._Server(('127.0.0.1', 0), FaultHandler)
        self.server.daemon_threads = False
        self.server.fault, self.server.puts = '', 0
        self.server.downloads = []
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = 'http://127.0.0.1:%s' % self.server.server_port
        self.admin = remote.Client(self.base)
        self.admin.login('test-key')
        self.senders = []

    def tearDown(self):
        for sender in self.senders:
            sender.stop()
            sender._shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('server state did not settle')
            time.sleep(.01)

    def transfer(self, contents, limit=1):
        grant, secret, space = self.store.create_transfer(max_downloads=limit)
        files = []
        for i, payload in enumerate(contents):
            name = '%s.bin' % i
            with open(os.path.join(self.store.space_dir(space), name), 'wb') as f:
                f.write(payload)
            fid = self.store.add_file(space_id=space, stored_name=name, display_name=name,
                                      size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
            files.append(dict(self.store.get_file(fid), name=name))
        return grant['id'], secret, files

    def receiver(self, secret):
        cli = remote.Client(self.base)
        cli.login(secret)
        return cli

    def download(self, cli, fid, rng=None):
        headers = {'Range': rng} if rng else {}
        with cli.request('GET', '/api/download?id=' + fid, headers=headers, raw=True) as resp:
            return resp.read()

    def downloads(self, gid):
        return self.store.get_grant(gid)['downloads']

    def test_receivers_are_isolated_and_relogin_keeps_progress(self):
        gid, secret, files = self.transfer([b'abcd', b'efgh'], limit=2)
        a, b = self.receiver(secret), self.receiver(secret)
        self.download(a, files[0]['id'])
        self.download(b, files[1]['id'])
        self.wait_for(lambda: len(self.store._query('SELECT * FROM fetch_ranges')) == 2)
        self.assertEqual(self.downloads(gid), 0)
        a.login(secret)
        self.download(a, files[1]['id'])
        self.wait_for(lambda: self.downloads(gid) == 1)
        self.download(b, files[0]['id'])
        self.wait_for(lambda: self.downloads(gid) == 2)
        self.assertFalse(self.store.grant_state(gid)[0])

    def test_overlapping_ranges_head_and_done_do_not_complete_pickup(self):
        gid, secret, files = self.transfer([b'abcd'])
        c, fid = self.receiver(secret), files[0]['id']
        with c.request('HEAD', '/api/download?id=' + fid, raw=True) as resp:
            self.assertEqual(resp.read(), b'')
        c.request('POST', '/api/done', {})
        self.assertEqual(self.downloads(gid), 0)
        for rng in ('bytes=0-1', 'bytes=0-1', 'bytes=1-2'):
            self.download(c, fid, rng)
        self.wait_for(lambda: bool(self.store._query('SELECT * FROM fetch_ranges')))
        self.assertEqual(self.downloads(gid), 0)
        self.download(c, fid, 'bytes=3-3')
        self.wait_for(lambda: self.downloads(gid) == 1)

    def test_zero_byte_files_must_each_be_requested(self):
        gid, secret, files = self.transfer([b'', b''])
        c = self.receiver(secret)
        self.download(c, files[0]['id'])
        self.wait_for(lambda: bool(self.store._query('SELECT * FROM fetch_ranges')))
        self.assertEqual(self.downloads(gid), 0)
        self.download(c, files[1]['id'])
        self.wait_for(lambda: self.downloads(gid) == 1)

    def test_real_download_reset_preserves_progress_and_resume_counts(self):
        payload = b'x' * (3 * app.CHUNK_SIZE)
        gid, secret, files = self.transfer([payload])
        c = self.receiver(secret)
        self.server.fault = 'download'
        dest = os.path.join(self.tmp.name, 'resume')
        with self.assertRaises(common.CliError):
            remote.download_file(c, files[0], dest, True, lambda *a: None)
        self.wait_for(lambda: bool(self.store._query('SELECT * FROM fetch_ranges')))
        ranges = json.loads(self.store._one('SELECT ranges FROM fetch_ranges')['ranges'])
        self.assertEqual(ranges, [[0, app.CHUNK_SIZE]])
        self.assertEqual(self.downloads(gid), 0)
        result = remote.download_file(c, files[0], dest, True, lambda *a: None)
        with open(result, 'rb') as f:
            self.assertEqual(f.read(), payload)
        self.wait_for(lambda: self.downloads(gid) == 1)
        self.assertEqual(self.server.downloads, [None, 'bytes=%s-' % app.CHUNK_SIZE])

    def test_preexisting_untrusted_partial_is_recovered_before_counting(self):
        gid, secret, files = self.transfer([b'abcd'])
        c = self.receiver(secret)
        dest = os.path.join(self.tmp.name, 'preexisting')
        os.makedirs(dest)
        with open(os.path.join(dest, '0.bin.part'), 'wb') as f:
            f.write(b'xx')
        target = remote.download_file(c, files[0], dest, True, lambda *a: None)
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'abcd')
        self.wait_for(lambda: self.downloads(gid) == 1)
        self.assertEqual(self.server.downloads, ['bytes=0-1', 'bytes=2-'])

    def test_progress_endpoint_enforces_permissions_and_receiver_isolation(self):
        gid, secret, files = self.transfer([b'abcd'])
        route = '/api/download?id=' + files[0]['id'] + '&progress=1'
        with self.assertRaises(common.CliError) as err:
            remote.Client(self.base).request('GET', route)
        self.assertEqual(err.exception.status, 401)
        a, b = self.receiver(secret), self.receiver(secret)
        self.download(a, files[0]['id'], 'bytes=0-1')
        self.wait_for(lambda: bool(self.store._query('SELECT * FROM fetch_ranges')))
        self.assertEqual(a.request('GET', route)['ranges'], [[0, 2]])
        self.assertEqual(b.request('GET', route)['ranges'], [])
        wrong, wrong_secret, wrong_files = self.transfer([b'efgh'])
        with self.assertRaises(common.CliError) as err:
            self.receiver(wrong_secret).request('GET', route)
        self.assertEqual(err.exception.status, 403)
        grant, upload_secret = self.store.create_grant('share', self.store.get_grant(gid)['space_id'],
                                                      perm='upload')
        with self.assertRaises(common.CliError) as err:
            self.receiver(upload_secret).request('GET', route)
        self.assertEqual(err.exception.status, 403)

    def test_pickup_ranges_survive_database_reopen(self):
        gid, secret, files = self.transfer([b'abcd'])
        c = self.receiver(secret)
        self.download(c, files[0]['id'], 'bytes=0-1')
        self.wait_for(lambda: bool(self.store._query('SELECT * FROM fetch_ranges')))
        self.store.close()
        self.store = Store(self.store.data_dir)
        app.STORE = self.store
        c.login(secret)
        self.download(c, files[0]['id'], 'bytes=2-3')
        self.wait_for(lambda: self.downloads(gid) == 1)

    def test_fetch_files_reuses_identity_across_separate_calls(self):
        gid, secret, files = self.transfer([b'abcd', b'efgh'])
        code = common.make_code(self.base, secret)
        dest = os.path.join(self.tmp.name, 'separate')
        remote.fetch_files(code, dest, only=[files[0]['name']], on_progress=lambda *a: None)
        self.assertEqual(self.downloads(gid), 0)
        remote.fetch_files(code, dest, only=[files[1]['name']], on_progress=lambda *a: None)
        self.wait_for(lambda: self.downloads(gid) == 1)
        cookies = os.listdir(os.path.join(self.tmp.name, 'receivers'))
        self.assertEqual(len(cookies), 1)
        if os.name != 'nt':
            self.assertEqual(os.stat(os.path.join(self.tmp.name, 'receivers', cookies[0])).st_mode & 0o777,
                             0o600)

    def test_partial_then_full_pickup_finishes_missing_files_last(self):
        gid, secret, files = self.transfer([b'abcd', b'efgh'])
        code = common.make_code(self.base, secret)
        dest = os.path.join(self.tmp.name, 'partial-full')
        remote.fetch_files(code, dest, only=[files[0]['name']], on_progress=lambda *a: None)
        self.assertEqual(self.downloads(gid), 0)
        saved = remote.fetch_files(code, dest, on_progress=lambda *a: None)
        self.assertEqual(len(saved), 2)
        self.assertTrue(os.path.isfile(os.path.join(dest, files[1]['name'])))
        self.wait_for(lambda: self.downloads(gid) == 1)

    def test_direct_partial_then_full_pickup_keeps_listener_until_last_file(self):
        paths = []
        for i in (0, 1):
            path = os.path.join(self.tmp.name, '%s.bin' % i)
            with open(path, 'wb') as f:
                f.write(b'abcd')
            paths.append(path)
        sender = direct.DirectSender(paths, host='127.0.0.1')
        self.senders.append(sender)
        sender.prepare_hashes()
        sender.start()
        code = common.make_code('http://127.0.0.1:%s' % sender.port, sender.token)
        dest = os.path.join(self.tmp.name, 'direct-partial-full')
        remote.fetch_files(code, dest, only=['1.bin'], on_progress=lambda *a: None)
        self.assertFalse(sender.finished.is_set())
        saved = remote.fetch_files(code, dest, on_progress=lambda *a: None)
        self.assertEqual(len(saved), 2)
        self.assertEqual(sender.wait(5), 'done')

    def test_final_upload_response_loss_is_idempotent(self):
        for fault in ('upload_headers', 'upload_body'):
            for size in (0, 3, 5):
                with self.subTest(fault=fault, size=size):
                    gid, secret, files = self.transfer([])
                    space = self.store.get_grant(gid)['space_id']
                    path = os.path.join(self.tmp.name, 'upload.bin')
                    with open(path, 'wb') as f:
                        f.write(b'x' * size)
                    self.server.fault = fault
                    before = self.server.puts
                    with patch.object(remote, 'CHUNK', 4), patch.object(remote.time, 'sleep'):
                        receipt = remote.upload_file(self.admin, space, path, lambda *a: None)
                    published = self.store.list_files(space)
                    self.assertEqual(len(published), 1)
                    self.assertEqual(receipt['id'], published[0]['id'])
                    self.assertEqual(self.server.puts - before, (2 if size <= 4 else 3))

    def test_partial_upload_response_loss_realigns_offset(self):
        gid, secret, files = self.transfer([])
        space = self.store.get_grant(gid)['space_id']
        path = os.path.join(self.tmp.name, 'upload.bin')
        with open(path, 'wb') as f:
            f.write(b'x' * 9)
        self.server.fault = 'upload_partial'
        with patch.object(remote, 'CHUNK', 4), patch.object(remote.time, 'sleep'):
            remote.upload_file(self.admin, space, path, lambda *a: None)
        self.assertEqual(len(self.store.list_files(space)), 1)
        self.assertEqual(self.server.puts, 4)

    def test_receipt_is_persistent_and_bound_to_owner_and_metadata(self):
        route = '/api/upload?name=receipt.bin&id=receipt&offset=0&total=4'
        first = self.admin.request('PUT', route, b'abcd')
        self.store.close()
        self.store = Store(self.store.data_dir)
        app.STORE = self.store
        second = self.admin.request('PUT', route, b'abcd')
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.list_files(self.default)), 1)
        other = self.receiver('test-key')
        with self.assertRaises(common.CliError) as err:
            other.request('PUT', route, b'abcd')
        self.assertEqual(err.exception.status, 403)
        with self.assertRaises(common.CliError) as err:
            self.admin.request('PUT', route.replace('receipt.bin', 'different.bin'), b'abcd')
        self.assertEqual(err.exception.status, 409)

    def test_direct_suffix_and_done_require_full_coverage(self):
        path = os.path.join(self.tmp.name, 'direct.bin')
        with open(path, 'wb') as f:
            f.write(b'abcd')
        sender = direct.DirectSender([path], host='127.0.0.1', receivers=2)
        self.senders.append(sender)
        sender.prepare_hashes()
        sender.start()
        base = 'http://127.0.0.1:%s' % sender.port
        a, b = remote.Client(base), remote.Client(base)
        a.login(sender.token)
        b.login(sender.token)
        self.download(a, '0', 'bytes=3-3')
        self.assertFalse(a.request('POST', '/api/done', {})['complete'])
        self.assertEqual(sender._done, 0)
        self.download(a, '0', 'bytes=0-1')
        self.download(a, '0', 'bytes=0-1')
        self.assertFalse(a.request('POST', '/api/done', {})['complete'])
        a.login(sender.token)
        self.download(a, '0', 'bytes=2-2')
        self.wait_for(lambda: sender._done == 1)
        a.request('POST', '/api/done', {})
        self.assertEqual(sender._done, 1)
        self.download(b, '0')
        self.assertEqual(sender.wait(5), 'done')

    def test_direct_listener_is_available_while_hashing(self):
        path = os.path.join(self.tmp.name, 'hashing.bin')
        with open(path, 'wb') as f:
            f.write(b'abcd')
        sender = direct.DirectSender([path], host='127.0.0.1')
        self.senders.append(sender)
        entered, release = threading.Event(), threading.Event()

        def slow_hash(path):
            entered.set()
            if not release.wait(5):
                raise OSError('test hash timed out')
            return hashlib.sha256(b'abcd').hexdigest()

        sender.start()
        c = remote.Client('http://127.0.0.1:%s' % sender.port)
        with patch.object(direct, 'sha256_file', slow_hash):
            sender.start_hashing()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(common.CliError) as err:
                    c.login(sender.token)
                self.assertEqual(err.exception.status, 503)
                self.assertEqual(err.exception.headers['Retry-After'], '2')
            finally:
                release.set()
                self.assertTrue(sender._ready.wait(5))
        c.login(sender.token)
        self.assertEqual(self.download(c, '0'), b'abcd')
        self.assertEqual(sender.wait(5), 'done')

    def test_cancelled_server_upload_is_revoked(self):
        path = os.path.join(self.tmp.name, 'cancel.bin')
        with open(path, 'wb') as f:
            f.write(b'x' * 9)
        cancelled = threading.Event()
        with patch.object(remote, 'CHUNK', 4):
            with self.assertRaises(common.CliError):
                remote.send_files(self.base, 'test-key', [path],
                                  on_progress=lambda *a: cancelled.set(),
                                  should_cancel=cancelled.is_set)
        transfers = self.store.list_transfers()
        self.assertEqual(len(transfers), 1)
        self.assertTrue(transfers[0]['revoked'])
        self.assertEqual(transfers[0]['files'], 0)
        self.store.cleanup_transfers()
        self.assertEqual(self.store.purge_orphan_parts(min_age=-1), 1)


if __name__ == '__main__':
    unittest.main()
