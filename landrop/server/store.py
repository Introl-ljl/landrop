"""One local SQLite model for shares, received content and desktop/CLI settings."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager

from landrop.common import (Cancelled, CliError, default_download_dir, file_version, safe_local_name,
                            safe_relpath, unique_path)

TEXT_LIMIT = 10 * 1024 * 1024
TEXT_COUNT = 100
PARTIAL_TTL = 86400
SESSION_TTL = 7 * 86400
_UNSET = object()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS codes(code TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS shares(
 id TEXT PRIMARY KEY, label TEXT NOT NULL, code TEXT NOT NULL UNIQUE,
 roots TEXT NOT NULL, enabled INTEGER NOT NULL, allow_upload INTEGER NOT NULL,
 public_received INTEGER NOT NULL, collection_base TEXT NOT NULL, collection_dir TEXT NOT NULL,
 expires_at REAL, version INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
 token TEXT PRIMARY KEY, share_id TEXT NOT NULL REFERENCES shares(id) ON DELETE CASCADE,
 version INTEGER NOT NULL, visitor TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS uploads(
 id TEXT PRIMARY KEY, share_id TEXT NOT NULL, visitor TEXT NOT NULL, name TEXT NOT NULL,
 total INTEGER NOT NULL, received INTEGER NOT NULL, expected TEXT NOT NULL, directory TEXT NOT NULL,
 target TEXT NOT NULL DEFAULT '', digest TEXT NOT NULL DEFAULT '',
 complete INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
 target_key TEXT NOT NULL DEFAULT '', cancelled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS texts(
 id TEXT PRIMARY KEY, share_id TEXT NOT NULL REFERENCES shares(id) ON DELETE CASCADE,
 received INTEGER NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL, size INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS digests(path TEXT PRIMARY KEY, version TEXT NOT NULL, digest TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS uploads_share ON uploads(share_id, complete);
CREATE INDEX IF NOT EXISTS uploads_target ON uploads(target, complete);
CREATE INDEX IF NOT EXISTS texts_share ON texts(share_id, created_at);
"""


class OverlapError(CliError):
    pass


class Store:
    def __init__(self, data_dir):
        self.data_dir = os.path.realpath(os.path.expanduser(data_dir))
        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "shares.sqlite3")
        self.partial_dir = os.path.join(self.data_dir, "partial")
        os.makedirs(self.partial_dir, mode=0o700, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.RLock()
        self._publishing = set()
        fresh = not os.path.exists(self.db_path)
        if fresh:
            self._backup_legacy()
        self.db.executescript(SCHEMA)
        self._migrate()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        if fresh:
            self._import_preferences()

    def _migrate(self):
        for table, additions in (("uploads", {"target_key": "TEXT NOT NULL DEFAULT ''",
                                                "cancelled": "INTEGER NOT NULL DEFAULT 0"}),
                                  ("texts", {"size": "INTEGER NOT NULL DEFAULT 0"})):
            columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for name, definition in additions.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    if name == "size":
                        self.db.execute("UPDATE texts SET size=length(CAST(content AS BLOB))")

    @property
    def db(self):
        if not getattr(self._local, "db", None):
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.db = conn
        return self._local.db

    def close(self):
        if getattr(self._local, "db", None):
            self._local.db.close()
            self._local.db = None

    @contextmanager
    def transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _backup_legacy(self):
        old = os.path.join(self.data_dir, "state.sqlite3")
        if not os.path.isfile(old):
            return
        dest = os.path.join(self.data_dir, "legacy-backup", time.strftime("%Y%m%d-%H%M%S") +
                            "-" + secrets.token_hex(3))
        os.makedirs(dest, mode=0o700)
        source = sqlite3.connect(old)
        backup = sqlite3.connect(os.path.join(dest, "state.sqlite3"))
        try:
            source.backup(backup)
        finally:
            backup.close()
            source.close()
        for name in ("desktop.json", "admin-key.txt"):
            path = os.path.join(self.data_dir, name)
            if os.path.isfile(path):
                shutil.copy2(path, dest)

    def _import_preferences(self):
        try:
            with open(os.path.join(self.data_dir, "desktop.json"), encoding="utf-8") as stream:
                old = json.load(stream)
            values = {key: old[key] for key in ("theme", "recv_dir") if key in old}
            if str(old.get("port", "")).isdigit() and 1 <= int(old["port"]) <= 65535:
                values["port"] = int(old["port"])
            self.configure(**values)
        except (OSError, ValueError, TypeError, CliError):
            pass

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    def settings(self):
        downloads = default_download_dir()
        values = {"collect_dir": os.path.join(downloads, "LANDrop"), "recv_dir": downloads,
                  "port": 8000, "ip": "", "theme": "system", "text_preview_mib": 10,
                  "image_preview_mib": 20, "receive_history": []}
        values.update(self.get_meta("settings", {}))
        return values

    def configure(self, **values):
        if set(values) - set(self.settings()):
            raise CliError("未知设置")
        try:
            for key in ("port", "text_preview_mib", "image_preview_mib"):
                if key in values:
                    value = int(values[key])
                    if not 1 <= value <= (65535 if key == "port" else 1024):
                        raise ValueError
                    values[key] = value
            if values.get("ip"):
                ipaddress.ip_address(values["ip"])
        except (TypeError, ValueError):
            raise CliError("端口、地址或预览上限不正确") from None
        for key in ("collect_dir", "recv_dir"):
            if key in values:
                values[key] = os.path.realpath(os.path.expanduser(values[key]))
        if "theme" in values and values["theme"] not in ("system", "light", "dark"):
            raise CliError("未知主题")
        with self.transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            settings = json.loads(row[0]) if row else {}
            settings.update(values)
            db.execute("INSERT OR REPLACE INTO meta VALUES('settings',?)", (json.dumps(settings),))
        return self.settings()

    def record_receive(self, count, directory):
        with self.transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            settings = json.loads(row[0]) if row else {}
            history = settings.get("receive_history", [])
            settings["receive_history"] = [{"count": count, "directory": directory,
                                             "time": time.time()}] + history[:4]
            db.execute("INSERT OR REPLACE INTO meta VALUES('settings',?)", (json.dumps(settings),))

    @staticmethod
    def _share(row):
        if row is None:
            raise CliError("分享不存在")
        item = dict(row)
        item["roots"] = json.loads(item["roots"])
        item["active"] = bool(item["enabled"] and
                              (not item["expires_at"] or item["expires_at"] > time.time()))
        return item

    def shares(self):
        return [self._share(row) for row in self.db.execute("SELECT * FROM shares ORDER BY created_at DESC")]

    def share(self, share_id):
        return self._share(self.db.execute("SELECT * FROM shares WHERE id=?", (share_id,)).fetchone())

    def _new_code(self, db):
        for _ in range(1000):
            code = f"{secrets.randbelow(1000000):06d}"
            try:
                db.execute("INSERT INTO codes VALUES(?)", (code,))
                return code
            except sqlite3.IntegrityError:
                pass
        raise CliError("无法分配未使用的授权码")

    @staticmethod
    def _contains(parent, child):
        try:
            return os.path.commonpath((parent, child)) == parent
        except ValueError:
            return False

    def _protected(self, path):
        if os.path.basename(path).startswith(".landrop-") or path.endswith(".landrop-pending"):
            return True
        if any(self._contains(os.path.join(self.data_dir, name), path) for name in
               ("partial", "receivers", "legacy-backup")):
            return True
        return os.path.dirname(path) == self.data_dir and (
            os.path.basename(path) in ("desktop.json", "admin-key.txt") or
            any(os.path.basename(path).startswith(name) for name in
                ("shares.sqlite3", "state.sqlite3", "service.lock", "diagnostics.log")))

    def _roots(self, paths):
        roots = []
        for path in paths:
            path = os.path.realpath(os.path.expanduser(path))
            if self._protected(path) or (not os.path.isfile(path) and not os.path.isdir(path)):
                raise CliError(f"文件或目录不可用：{path}")
            if not any(root["path"] == path for root in roots):
                roots.append({"path": path, "directory": os.path.isdir(path)})
        return roots

    def _overlaps(self, shares):
        return {(source["id"], root["path"], target["id"], target["collection_dir"])
                for source in shares for root in source["roots"] if root["directory"]
                for target in shares if target["collection_dir"] and not target["public_received"]
                if self._contains(root["path"], target["collection_dir"]) or
                self._contains(target["collection_dir"], root["path"])}

    def save_share(self, share_id=None, paths=None, label=None, allow_upload=None,
                   public_received=None, collection_base=None, expires_at=_UNSET,
                   enabled=None, confirm_overlap=False, text=None, expected_version=None):
        created_dir = ""
        try:
            with self.transaction() as db:
                existing = self.shares()
                old = self.share(share_id) if share_id else None
                if old and expected_version is not None and old["version"] != expected_version:
                    raise CliError("分享已被其他本机操作修改，请刷新后重试", 409)
                item = dict(old) if old else {
                    "id": secrets.token_hex(12), "label": "", "roots": [], "enabled": True,
                    "allow_upload": False, "public_received": False, "collection_base": "",
                    "collection_dir": "", "expires_at": None, "version": 0, "created_at": time.time()}
                if paths is not None:
                    item["roots"] = self._roots(paths)
                if label is not None:
                    item["label"] = str(label).strip()[:100]
                if not item["label"]:
                    item["label"] = (os.path.basename(item["roots"][0]["path"]) if
                                     item["roots"] else "收集") or "分享"
                for key, value in (("allow_upload", allow_upload), ("public_received", public_received),
                                   ("enabled", enabled)):
                    if value is not None:
                        item[key] = bool(value)
                if old and old["expires_at"] and old["expires_at"] <= time.time() and enabled is None:
                    item["enabled"] = False
                base = os.path.realpath(os.path.expanduser(collection_base or
                                       item["collection_base"] or self.settings()["collect_dir"]))
                if self._protected(base):
                    raise CliError("程序私有目录不能用于收集")
                if (item["allow_upload"] or item["collection_dir"]) and (
                        not item["collection_dir"] or base != item["collection_base"]):
                    os.makedirs(base, exist_ok=True)
                    name = safe_local_name(item["label"]) + "_" + time.strftime("%Y%m%d-%H%M%S")
                    directory = unique_path(base, name)
                    os.mkdir(directory)
                    item["collection_dir"] = created_dir = directory
                item["collection_base"] = base
                if expires_at is not _UNSET:
                    item["expires_at"] = expires_at
                changed = [s for s in existing if s["id"] != item["id"]] + [item]
                if not confirm_overlap and self._overlaps(changed) - self._overlaps(existing):
                    raise OverlapError("共享目录与私密收集目录重叠，目录分享可能公开当前及后续收到的文件，请确认")
                item["code"] = old["code"] if old else self._new_code(db)
                item["version"] += 1
                values = (item["label"], item["code"], json.dumps(item["roots"]),
                          item["enabled"], item["allow_upload"], item["public_received"], base,
                          item["collection_dir"], item["expires_at"], item["version"], item["created_at"], item["id"])
                if old:
                    db.execute("UPDATE shares SET label=?,code=?,roots=?,enabled=?,allow_upload=?,"
                               "public_received=?,collection_base=?,collection_dir=?,expires_at=?,"
                               "version=?,created_at=? WHERE id=?", values)
                else:
                    db.execute("INSERT INTO shares VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (item["id"],) + values[:-1])
                if text is not None:
                    self._insert_text(db, item["id"], text, False)
            return self.share(item["id"])
        except BaseException:
            if created_dir:
                try:
                    os.rmdir(created_dir)
                except OSError:
                    pass
            raise

    def set_enabled(self, share_id, enabled):
        with self.transaction() as db:
            item = self.share(share_id)
            if enabled and item["expires_at"] and item["expires_at"] <= time.time():
                raise CliError("分享已过期，请先调整有效期")
            db.execute("UPDATE shares SET enabled=?,version=version+1 WHERE id=?", (bool(enabled), share_id))
        return self.share(share_id)

    def rotate(self, share_id):
        with self.transaction() as db:
            self.share(share_id)
            db.execute("UPDATE shares SET code=?,version=version+1 WHERE id=?", (self._new_code(db), share_id))
        return self.share(share_id)

    def delete_share(self, share_id):
        with self.transaction() as db:
            self.share(share_id)
            db.execute("DELETE FROM shares WHERE id=?", (share_id,))
            db.execute("DELETE FROM uploads WHERE share_id=? AND complete=1", (share_id,))

    @staticmethod
    def _hash(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def login(self, code, visitor):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM shares WHERE code=?", (code,)).fetchone()
            if not row or not self._share(row)["active"]:
                raise CliError("授权码无效或分享已停用")
            token = secrets.token_urlsafe(24)
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (
                self._hash(token), row["id"], row["version"], self._hash(visitor), time.time() + SESSION_TTL))
        return token

    def auth(self, token):
        row = self.db.execute("SELECT * FROM sessions WHERE token=? AND expires_at>?",
                              (self._hash(token), time.time())).fetchone()
        if row:
            try:
                share = self.share(row["share_id"])
                if share["active"] and share["version"] == row["version"]:
                    return {"share": share, "visitor": row["visitor"], "version": row["version"],
                            "session": row["token"]}
            except CliError:
                pass
        return None

    def guard(self, auth, upload=False):
        item = self.share(auth["share"]["id"])
        session = self.db.execute("SELECT 1 FROM sessions WHERE token=? AND expires_at>?",
                                  (auth["session"], time.time())).fetchone()
        if (not session or not item["active"] or item["version"] != auth["version"] or
                (upload and not item["allow_upload"])):
            raise CliError("分享已变化或失去访问权限", 403)
        return item

    def logout(self, token):
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (self._hash(token),))

    @staticmethod
    def file_id(share_id, path):
        return hashlib.sha256((share_id + "\n" + path).encode()).hexdigest()[:32]

    def files(self, share_id, local=False):
        share = self.share(share_id)
        uploads = list(self.db.execute("SELECT * FROM uploads WHERE share_id=? AND complete=1", (share_id,)))
        result, used, names = [], set(), set()
        def add(path, name, explicit=False):
            if path in used or self._protected(path) or os.path.realpath(path) != path or not os.path.isfile(path):
                return
            with self._lock:
                received = self.db.execute("SELECT share_id,complete FROM uploads WHERE target=?", (path,)).fetchone()
                if received and (not received["complete"] or
                                 (not local and not explicit and received["share_id"] == share_id and
                                  not share["public_received"])):
                    return
            try:
                stat = os.stat(path)
                version = file_version(path)
            except OSError:
                return
            name = "/".join(safe_local_name(part) for part in name.split("/"))
            if name in names:
                stem, ext = os.path.splitext(name)
                n = 1
                while f"{stem} ({n}){ext}" in names:
                    n += 1
                name = f"{stem} ({n}){ext}"
            names.add(name)
            used.add(path)
            digest = self.db.execute("SELECT digest FROM digests WHERE path=? AND version=?", (path, version)).fetchone()
            result.append({"id": self.file_id(share_id, path), "name": name, "path": name,
                           "size": stat.st_size, "version": version, "sha256": digest[0] if digest else "",
                           "full_path": path})
        for root in share["roots"]:
            path = root["path"]
            if root["directory"] and os.path.realpath(path) == path:
                for directory, dirs, files in os.walk(path):
                    dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(directory, d)) and
                                     not self._protected(os.path.realpath(os.path.join(directory, d))))
                    for name in sorted(files):
                        full = os.path.join(directory, name)
                        if self._contains(path, os.path.realpath(full)):
                            add(full, os.path.basename(path) + "/" + os.path.relpath(full, path).replace(os.sep, "/"))
            elif not root["directory"]:
                add(path, os.path.basename(path), explicit=True)
        if local or share["public_received"]:
            for row in uploads:
                add(row["target"], os.path.relpath(row["target"], row["directory"]).replace(os.sep, "/"))
        return result

    def file(self, share_id, file_id):
        for item in self.files(share_id):
            if item["id"] == file_id:
                return item
        raise CliError("文件不可用或未获授权", 404)

    def digest(self, item, guard=lambda: None):
        path, version = item["full_path"], item["version"]
        cached = self.db.execute("SELECT digest FROM digests WHERE path=? AND version=?", (path, version)).fetchone()
        if cached:
            return cached[0]
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                guard()
                digest.update(block)
        if file_version(path) != version:
            raise CliError("文件内容已变化，请重试", 409)
        value = digest.hexdigest()
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO digests VALUES(?,?,?)", (path, version, value))
        return value

    def texts(self, share_id, local=False):
        share = self.share(share_id)
        return [dict(row) for row in self.db.execute(
            "SELECT id,received,created_at,size FROM texts "
            "WHERE share_id=? AND (? OR received=0) ORDER BY created_at", (
                share_id, bool(local or share["public_received"])))]

    def text(self, share_id, text_id, local=False):
        share = self.share(share_id)
        row = self.db.execute("SELECT content FROM texts WHERE id=? AND share_id=? AND (? OR received=0)",
                              (text_id, share_id, bool(local or share["public_received"]))).fetchone()
        if not row:
            raise CliError("文本不可用或未获授权", 404)
        return row[0]

    def add_text(self, share_id, content, auth=None, guard=None):
        with self.transaction() as db:
            if auth:
                if auth["share"]["id"] != share_id:
                    raise CliError("文本未获授权", 403)
                self.guard(auth, upload=True)
            else:
                self.share(share_id)
            if guard:
                guard()
            return self._insert_text(db, share_id, content, bool(auth))

    def _insert_text(self, db, share_id, content, received):
        if not isinstance(content, str) or not content.strip() or len(content.encode()) > TEXT_LIMIT:
            raise CliError("文本不能为空，单条上限为 10 MiB")
        count = db.execute("SELECT COUNT(*) FROM texts WHERE share_id=?", (share_id,)).fetchone()[0]
        if count >= TEXT_COUNT:
            raise CliError("已达到 100 条文本上限，请在本机清理后再提交")
        text_id = secrets.token_hex(12)
        db.execute("INSERT INTO texts(id,share_id,received,content,created_at,size) VALUES(?,?,?,?,?,?)",
                   (text_id, share_id, received, content, time.time(), len(content.encode("utf-8"))))
        return text_id

    def clear_texts(self, share_id):
        with self.transaction() as db:
            self.share(share_id)
            db.execute("DELETE FROM texts WHERE share_id=?", (share_id,))

    def _partial(self, upload_id):
        return os.path.join(self.partial_dir, upload_id + ".part")

    def _remove_partial(self, upload_id):
        try:
            os.remove(self._partial(upload_id))
        except FileNotFoundError:
            pass

    def upload(self, auth, upload_id, name, total, expected, offset, block, guard):
        if (not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", upload_id) or
                (expected and not re.fullmatch(r"[0-9a-f]{64}", expected)) or
                not isinstance(total, int) or not isinstance(offset, int) or
                not 0 <= offset <= total < 2 ** 63):
            raise CliError("上传参数不正确")
        share = self.guard(auth, upload=True)
        name = safe_relpath(name).replace(os.sep, "/")
        part = self._partial(upload_id)
        with self.transaction() as db:
            self.guard(auth, upload=True)
            row = db.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
            if row:
                if (row["share_id"] != share["id"] or row["visitor"] != auth["visitor"] or
                        row["name"] != name or row["total"] != total or row["expected"] != expected):
                    raise CliError("上传标识或文件信息不匹配")
                if row["cancelled"]:
                    raise Cancelled("上传已取消")
                if row["complete"]:
                    return {"complete": True, "sha256": row["digest"], "name": os.path.basename(row["target"])}
                actual = os.path.getsize(part) if os.path.isfile(part) else 0
            else:
                if offset:
                    return {"offset": 0, "conflict": True}
                actual = 0
                db.execute("INSERT INTO uploads(id,share_id,visitor,name,total,received,expected,directory,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (upload_id, share["id"], auth["visitor"], name,
                                                       total, 0, expected, share["collection_dir"], time.time()))
            if actual != offset:
                return {"offset": actual, "conflict": True}
            if offset + len(block) > total:
                raise CliError("上传数据超出文件大小")
            with open(part, "ab") as stream:
                stream.write(block)
            actual += len(block)
            db.execute("UPDATE uploads SET received=?,updated_at=? WHERE id=?", (actual, time.time(), upload_id))
        if actual < total:
            return {"complete": False, "offset": actual}
        return self._finish_upload(auth, upload_id, guard)

    def _finish_upload(self, auth, upload_id, guard):
        with self._lock:
            if upload_id in self._publishing:
                return {"complete": False, "publishing": True}
            self._publishing.add(upload_id)
        target, owned, published = "", False, False
        def check():
            guard()
            pending = self.db.execute("SELECT cancelled FROM uploads WHERE id=?", (upload_id,)).fetchone()
            if not pending or pending[0]:
                raise Cancelled("上传已取消")
        try:
            row = self.db.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
            if row["complete"]:
                return {"complete": True, "sha256": row["digest"], "name": os.path.basename(row["target"])}
            part = self._partial(upload_id)
            relative = safe_relpath(row["name"])
            directory = os.path.join(row["directory"], os.path.dirname(relative))
            if not self._contains(row["directory"], os.path.realpath(directory)):
                raise CliError("收集目录不可用")
            os.makedirs(directory, exist_ok=True)
            with self._lock:
                while True:
                    target = unique_path(directory, os.path.basename(relative))
                    try:
                        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        owned = True
                        break
                    except FileExistsError:
                        continue
                with self.transaction() as db:
                    stat = os.fstat(descriptor)
                    db.execute("UPDATE uploads SET target=?,target_key=? WHERE id=?", (
                        target, f"{stat.st_dev}:{stat.st_ino}", upload_id))
            with os.fdopen(descriptor, "wb") as dest, open(part, "rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    check()
                    dest.write(block)
                dest.flush()
                os.fsync(dest.fileno())
            version = file_version(target)
            checksum = hashlib.sha256()
            with open(target, "rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    check()
                    checksum.update(block)
            digest = checksum.hexdigest()
            check()
            if (file_version(target) != version or
                    (row["expected"] and digest != row["expected"])):
                raise CliError("文件校验失败，请重新上传", 422)
            with self.transaction() as db:
                check()
                self.guard(auth, upload=True)
                db.execute("UPDATE uploads SET complete=1,digest=?,updated_at=? WHERE id=?", (
                    digest, time.time(), upload_id))
                db.execute("INSERT OR REPLACE INTO digests VALUES(?,?,?)", (target, version, digest))
            published = True
            self._remove_partial(upload_id)
            return {"complete": True, "sha256": digest, "name": os.path.basename(target)}
        except BaseException as exc:
            if owned and not published:
                try:
                    os.remove(target)
                except OSError:
                    pass
                with self.transaction() as db:
                    db.execute("UPDATE uploads SET target='',target_key='' WHERE id=? AND complete=0", (upload_id,))
            if isinstance(exc, Cancelled) or getattr(exc, "status", None) == 422:
                with self.transaction() as db:
                    db.execute("DELETE FROM uploads WHERE id=? AND complete=0", (upload_id,))
                self._remove_partial(upload_id)
            raise
        finally:
            with self._lock:
                self._publishing.discard(upload_id)

    def abort_upload(self, auth, upload_id):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
            if not row or row["complete"]:
                return
            if row["share_id"] != auth["share"]["id"] or row["visitor"] != auth["visitor"]:
                raise CliError("没有权限取消该上传")
            if upload_id in self._publishing:
                db.execute("UPDATE uploads SET cancelled=1 WHERE id=?", (upload_id,))
                return
            db.execute("DELETE FROM uploads WHERE id=?", (upload_id,))
            self._remove_partial(upload_id)

    def cleanup(self):
        with self.transaction() as db:
            for row in list(db.execute("SELECT id,target,target_key FROM uploads WHERE complete=0 AND updated_at<?",
                                        (time.time() - PARTIAL_TTL,))):
                if row["id"] in self._publishing:
                    continue
                self._remove_partial(row["id"])
                if row["target"]:
                    try:
                        stat = os.stat(row["target"], follow_symlinks=False)
                        if row["target_key"] == f"{stat.st_dev}:{stat.st_ino}":
                            os.remove(row["target"])
                    except FileNotFoundError:
                        pass
                db.execute("DELETE FROM uploads WHERE id=?", (row["id"],))
            db.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
