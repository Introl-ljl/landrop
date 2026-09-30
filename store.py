#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LAN Drop 持久状态层（SQLite）。

长期状态统一放在数据根目录 ``<data-dir>/``：

* ``state.sqlite3`` —— 空间、授权、访客、会话、文件索引、分块上传状态
* ``files/<空间>/`` —— 已完成文件的落盘位置（空间根目录可另行指定）
* ``partial/``       —— 上传中的分块，不属于任何共享目录可见范围
* ``trash/``         —— 删除的文件移到这里，便于人工恢复

授权判定只依据数据库记录，绝不采信客户端提交的用户名、文件名或 owner 字段。
访客身份是"浏览器匿名主体"：secret 只下发到 HttpOnly Cookie，库中仅存摘要。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata

PBKDF2_ROUNDS = 120_000            # 人类口令的派生轮数
SESSION_TTL = 7 * 86400            # 会话有效期（秒）
VISITOR_TTL = 400 * 86400          # 访客 Cookie 有效期（秒）
VALID_PERMS = ("upload", "delete_own", "full")
VALID_MODES = ("token", "password")
HASH_CHUNK = 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spaces (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  dir        TEXT NOT NULL UNIQUE,
  root       TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  space_id     TEXT REFERENCES spaces(id) ON DELETE CASCADE,
  label        TEXT NOT NULL DEFAULT '',
  perm         TEXT NOT NULL DEFAULT 'full',
  mode         TEXT NOT NULL DEFAULT 'token',
  secret_salt  TEXT,
  secret_hash  TEXT,
  token_hash   TEXT,
  created_at   REAL NOT NULL,
  expires_at   REAL,
  revoked      INTEGER NOT NULL DEFAULT 0,
  version      INTEGER NOT NULL DEFAULT 1,
  last_used_at REAL
);
CREATE INDEX IF NOT EXISTS idx_grants_token ON grants(token_hash);
CREATE INDEX IF NOT EXISTS idx_grants_space ON grants(space_id);
CREATE TABLE IF NOT EXISTS visitors (
  id           TEXT PRIMARY KEY,
  secret_hash  TEXT NOT NULL,
  created_at   REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  revoked      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash    TEXT PRIMARY KEY,
  visitor_id    TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
  grant_id      TEXT NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
  grant_version INTEGER NOT NULL,
  created_at    REAL NOT NULL,
  expires_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  id            TEXT PRIMARY KEY,
  space_id      TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
  stored_name   TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  size          INTEGER NOT NULL DEFAULT 0,
  sha256        TEXT,
  origin        TEXT NOT NULL DEFAULT 'upload',
  status        TEXT NOT NULL DEFAULT 'active',
  owner_visitor TEXT,
  owner_grant   TEXT,
  created_at    REAL NOT NULL,
  deleted_at    REAL,
  deleted_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_space ON files(space_id, status);
CREATE TABLE IF NOT EXISTS uploads (
  id            TEXT PRIMARY KEY,
  space_id      TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
  visitor_id    TEXT NOT NULL,
  grant_id      TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  total         INTEGER,
  received      INTEGER NOT NULL DEFAULT 0,
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL
);
"""


def now() -> float:
    return time.time()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def token_hash(token: str) -> str:
    """高熵密钥只做一次 SHA-256，无需慢派生。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    ).hex()


def slugify(name: str) -> str:
    """生成安全的目录名（空间内部标识）。"""
    name = unicodedata.normalize("NFKD", name or "")
    name = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return (name or "space")[:40]


# Windows 保留设备名：这些名字作为文件名会在 Windows 上出错
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_stored_name(name: str) -> str:
    """落盘文件名：去掉路径与控制字符，保留可读性；跨平台可写。"""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f")
    name = name.strip().strip(".")
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    if not name:
        name = "unnamed"
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = "_" + name
    if len(name.encode("utf-8")) > 200:
        stem, dot, ext = name.rpartition(".")
        ext = ("." + ext) if dot else ""
        keep = 220 - len(ext.encode("utf-8"))
        name = stem.encode("utf-8")[: max(keep, 1)].decode("utf-8", "ignore") + ext
    return name


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class Store:
    """SQLite 状态存储。所有方法都可从多线程调用（连接按线程隔离）。"""

    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.files_root = os.path.join(self.data_dir, "files")
        self.partial_dir = os.path.join(self.data_dir, "partial")
        self.trash_dir = os.path.join(self.data_dir, "trash")
        self.export_dir = os.path.join(self.data_dir, "exports")
        for d in (self.data_dir, self.files_root, self.partial_dir, self.trash_dir, self.export_dir):
            os.makedirs(d, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "state.sqlite3")
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    # ---------------------------------------------------------------- 连接
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _write(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self._conn().execute(sql, args)
            return cur

    def _query(self, sql: str, args=()):
        return self._conn().execute(sql, args).fetchall()

    def _one(self, sql: str, args=()):
        return self._conn().execute(sql, args).fetchone()

    # ---------------------------------------------------------------- meta
    def get_meta(self, key: str):
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str):
        self._write(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------------------------------------------------------------- 空间
    def list_spaces(self):
        return self._query("SELECT * FROM spaces ORDER BY created_at")

    def get_space(self, space_id: str):
        return self._one("SELECT * FROM spaces WHERE id=?", (space_id,))

    def space_dir(self, space_id: str) -> str:
        row = self.get_space(space_id)
        if not row:
            raise KeyError(space_id)
        root = row["root"] or self.files_root
        return os.path.join(root, row["dir"])

    def create_space(self, name: str, root: str | None = None):
        sid = "sp_" + secrets.token_hex(6)
        base = slugify(name)
        with self._write_lock:
            taken = {r["dir"] for r in self._query("SELECT dir FROM spaces")}
            slug = base
            i = 2
            while slug in taken:
                slug = f"{base}-{i}"
                i += 1
            self._write(
                "INSERT INTO spaces(id,name,dir,root,created_at) VALUES(?,?,?,?,?)",
                (sid, name or slug, slug, os.path.abspath(root) if root else None, now()),
            )
        os.makedirs(self.space_dir(sid), exist_ok=True)
        return sid

    # ---------------------------------------------------------------- 授权
    def create_grant(
        self,
        kind: str,
        space_id: str | None = None,
        perm: str = "full",
        mode: str = "token",
        label: str = "",
        password: str | None = None,
        token: str | None = None,
        expires_at: float | None = None,
    ):
        """返回 (grant_row, 明文 secret)。明文只在创建时返回一次。"""
        if kind not in ("admin", "share"):
            raise ValueError("kind")
        if perm not in VALID_PERMS:
            raise ValueError("perm")
        if mode not in VALID_MODES:
            raise ValueError("mode")
        gid = ("gk_" if kind == "admin" else "gs_") + secrets.token_hex(6)
        secret = None
        salt = hashv = None
        thash = None
        if mode == "password":
            secret = password or secrets.token_urlsafe(9)
            salt = secrets.token_hex(16)
            hashv = password_hash(secret, salt)
        else:
            secret = token or secrets.token_urlsafe(24)
            thash = token_hash(secret)
        self._write(
            "INSERT INTO grants(id,kind,space_id,label,perm,mode,secret_salt,secret_hash,"
            "token_hash,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (gid, kind, space_id, label, perm, mode, salt, hashv, thash, now(), expires_at),
        )
        return self.get_grant(gid), secret

    def get_grant(self, grant_id: str):
        return self._one("SELECT * FROM grants WHERE id=?", (grant_id,))

    def list_grants(self):
        return self._query(
            "SELECT g.*, s.name AS space_name FROM grants g "
            "LEFT JOIN spaces s ON s.id=g.space_id ORDER BY g.created_at"
        )

    def find_login_grant(self, secret: str):
        """按密钥/口令找可登录的授权；返回 grant 行或 None。"""
        if not secret:
            return None
        thash = token_hash(secret)
        row = self._one("SELECT * FROM grants WHERE mode='token' AND token_hash=?", (thash,))
        if row:
            return row
        for cand in self._query(
            "SELECT * FROM grants WHERE mode='password' AND secret_hash IS NOT NULL"
        ):
            digest = password_hash(secret, cand["secret_salt"])
            if secrets.compare_digest(digest, cand["secret_hash"]):
                return cand
        return None

    def grant_state(self, grant_id: str):
        """返回 (是否可用, 原因)。撤销/过期时立即拒绝，无需等待会话到期。"""
        g = self.get_grant(grant_id)
        if g is None:
            return False, "授权不存在"
        if g["revoked"]:
            return False, "授权已撤销"
        if g["expires_at"] and g["expires_at"] < now():
            return False, "授权已过期"
        return True, ""

    def touch_grant(self, grant_id: str):
        self._write("UPDATE grants SET last_used_at=? WHERE id=?", (now(), grant_id))

    def revoke_grant(self, grant_id: str) -> bool:
        cur = self._write("UPDATE grants SET revoked=1, version=version+1 WHERE id=?", (grant_id,))
        return cur.rowcount > 0

    def rotate_grant(self, grant_id: str):
        """轮换密钥：旧密钥立即失效，授权 ID 与权限保持不变。"""
        g = self.get_grant(grant_id)
        if g is None:
            return None
        if g["mode"] == "password":
            secret = secrets.token_urlsafe(9)
            salt = secrets.token_hex(16)
            self._write(
                "UPDATE grants SET secret_salt=?, secret_hash=?, version=version+1 WHERE id=?",
                (salt, password_hash(secret, salt), grant_id),
            )
        else:
            secret = secrets.token_urlsafe(24)
            self._write(
                "UPDATE grants SET token_hash=?, version=version+1 WHERE id=?",
                (token_hash(secret), grant_id),
            )
        return secret

    # ---------------------------------------------------------------- 访客
    def create_visitor(self) -> tuple[str, str]:
        vid = "v_" + secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        ts = now()
        self._write(
            "INSERT INTO visitors(id,secret_hash,created_at,last_seen_at) VALUES(?,?,?,?)",
            (vid, token_hash(secret), ts, ts),
        )
        return vid, secret

    def visitor_from_secret(self, secret: str):
        if not secret:
            return None
        row = self._one(
            "SELECT * FROM visitors WHERE secret_hash=? AND revoked=0", (token_hash(secret),)
        )
        return row

    def touch_visitor(self, visitor_id: str):
        self._write("UPDATE visitors SET last_seen_at=? WHERE id=?", (now(), visitor_id))

    # ---------------------------------------------------------------- 会话
    def create_session(self, visitor_id: str, grant_id: str, grant_version: int):
        token = secrets.token_urlsafe(32)
        ts = now()
        self._write(
            "INSERT INTO sessions(token_hash,visitor_id,grant_id,grant_version,created_at,expires_at)"
            " VALUES(?,?,?,?,?,?)",
            (token_hash(token), visitor_id, grant_id, grant_version, ts, ts + SESSION_TTL),
        )
        return token

    def session_from_token(self, token: str):
        """返回会话及其关联授权；授权被撤销/过期或版本变化时返回 None。"""
        if not token:
            return None
        row = self._one("SELECT * FROM sessions WHERE token_hash=?", (token_hash(token),))
        if row is None:
            return None
        if row["expires_at"] < now():
            self._write("DELETE FROM sessions WHERE token_hash=?", (row["token_hash"],))
            return None
        g = self.get_grant(row["grant_id"])
        if g is None or g["revoked"] or (g["expires_at"] and g["expires_at"] < now()):
            return None
        if g["version"] != row["grant_version"]:
            return None
        visitor = self._one("SELECT * FROM visitors WHERE id=?", (row["visitor_id"],))
        if visitor is None or visitor["revoked"]:
            return None
        return {
            "session": row,
            "grant": g,
            "visitor": visitor,
            "space_id": g["space_id"],
            "perm": g["perm"],
        }

    def delete_session(self, token: str):
        self._write("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))

    def purge_expired(self):
        ts = now()
        with self._write_lock:
            self._write("DELETE FROM sessions WHERE expires_at<?", (ts,))
            self._write(
                "DELETE FROM uploads WHERE updated_at<?", (ts - 3 * 86400,)
            )

    # ---------------------------------------------------------------- 文件
    def list_files(self, space_id: str, owner_visitor: str | None = None):
        """列出空间内文件；owner_visitor 非空时只列出该访客上传的文件。"""
        sql = "SELECT * FROM files WHERE space_id=? AND status='active'"
        args: list = [space_id]
        if owner_visitor:
            sql += " AND owner_visitor=?"
            args.append(owner_visitor)
        sql += " ORDER BY created_at DESC"
        return self._query(sql, args)

    def get_file(self, file_id: str):
        return self._one("SELECT * FROM files WHERE id=?", (file_id,))

    def add_file(self, **kw):
        fid = kw.get("id") or "f_" + secrets.token_hex(8)
        self._write(
            "INSERT INTO files(id,space_id,stored_name,display_name,size,sha256,origin,status,"
            "owner_visitor,owner_grant,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                fid,
                kw["space_id"],
                kw["stored_name"],
                kw["display_name"],
                kw.get("size", 0),
                kw.get("sha256"),
                kw.get("origin", "upload"),
                kw.get("status", "active"),
                kw.get("owner_visitor"),
                kw.get("owner_grant"),
                kw.get("created_at", now()),
            ),
        )
        return fid

    def mark_deleted(self, file_id: str, actor: str, status: str = "deleted"):
        self._write(
            "UPDATE files SET status=?, deleted_at=?, deleted_by=? WHERE id=?",
            (status, now(), actor, file_id),
        )

    def file_path(self, row) -> str:
        return os.path.join(self.space_dir(row["space_id"]), row["stored_name"])

    def unique_stored_name(self, space_id: str, name: str) -> str:
        """同名文件追加 (1)、(2)… 序号；已删除文件仍占用名字，避免歧义。"""
        directory = self.space_dir(space_id)
        base, ext = os.path.splitext(name)
        candidate = name
        i = 1
        while os.path.exists(os.path.join(directory, candidate)):
            candidate = f"{base} ({i}){ext}"
            i += 1
        return candidate

    def move_to_trash(self, row) -> str | None:
        """把文件移入 trash/，返回新路径（失败返回 None）。"""
        src = self.file_path(row)
        if not os.path.isfile(src):
            return None
        dest_dir = os.path.join(self.trash_dir, row["space_id"])
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, row["id"] + "__" + row["stored_name"])
        try:
            shutil.move(src, dest)
        except OSError:
            try:
                shutil.copy2(src, dest)
                os.remove(src)
            except OSError:
                return None
        return dest

    # ---------------------------------------------------------------- 分块
    def create_upload(self, upload_id: str, space_id: str, visitor_id: str, grant_id: str,
                      display_name: str, total: int | None):
        ts = now()
        self._write(
            "INSERT INTO uploads(id,space_id,visitor_id,grant_id,display_name,total,received,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,0,?,?)",
            (upload_id, space_id, visitor_id, grant_id, display_name, total, ts, ts),
        )

    def get_upload(self, upload_id: str):
        return self._one("SELECT * FROM uploads WHERE id=?", (upload_id,))

    def update_upload(self, upload_id: str, received: int, total: int | None = None):
        if total is None:
            self._write(
                "UPDATE uploads SET received=?, updated_at=? WHERE id=?",
                (received, now(), upload_id),
            )
        else:
            self._write(
                "UPDATE uploads SET received=?, total=?, updated_at=? WHERE id=?",
                (received, total, now(), upload_id),
            )

    def drop_upload(self, upload_id: str):
        self._write("DELETE FROM uploads WHERE id=?", (upload_id,))

    def part_path(self, upload_id: str) -> str:
        return os.path.join(self.partial_dir, upload_id + ".part")

    # ---------------------------------------------------------------- 管理员
    def admin_grant(self):
        return self._one("SELECT * FROM grants WHERE kind='admin' ORDER BY created_at LIMIT 1")

    def admin_key_file(self) -> str:
        return os.path.join(self.data_dir, "admin-key.txt")

    def init_admin(self, provided: str | None = None) -> tuple[str | None, bool]:
        """确保存在管理员密钥；返回 (明文密钥或 None, 是否新建/轮换)。

        明文只在生成或显式重置时返回，并写入 ``admin-key.txt``（0600）便于找回。
        """
        g = self.admin_grant()
        if g is None:
            secret = provided or secrets.token_urlsafe(18)
            self.create_grant(kind="admin", space_id=None, perm="full", mode="token",
                              label="管理员", token=secret)
            self._write_admin_key_file(secret if not provided else secret)
            return secret, True
        if provided:
            if secrets.compare_digest(token_hash(provided), g["token_hash"] or ""):
                return provided, False
            self._write(
                "UPDATE grants SET token_hash=?, version=version+1 WHERE id=?",
                (token_hash(provided), g["id"]),
            )
            self._write_admin_key_file(provided)
            return provided, True
        try:
            with open(self.admin_key_file(), "r", encoding="utf-8") as f:
                return f.read().strip() or None, False
        except OSError:
            return None, False

    def _write_admin_key_file(self, secret: str):
        path = self.admin_key_file()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(secret + "\n")
            os.chmod(path, 0o600)
        except OSError:
            pass

    # ---------------------------------------------------------------- 导入
    def import_directory(self, space_id: str, src_dir: str, recursive: bool = False) -> dict:
        """把已有目录里的文件复制并登记进空间；原目录保持不变。

        每个文件都做"复制 → 校验 SHA-256 → 登记"，任何异常都会记录到报告中，
        不会删除源文件，也不会覆盖已登记的文件。
        """
        report = {"imported": [], "skipped": [], "failed": [], "manifest": None}
        src_root = os.path.abspath(src_dir)
        if not os.path.isdir(src_root):
            report["failed"].append({"path": src_root, "error": "目录不存在"})
            return report
        entries = []
        if recursive:
            for base, _dirs, names in os.walk(src_root):
                for nm in sorted(names):
                    entries.append(os.path.join(base, nm))
        else:
            entries = [os.path.join(src_root, nm) for nm in sorted(os.listdir(src_root))]
        for path in entries:
            rel = os.path.relpath(path, src_root)
            base = os.path.basename(path)
            if base.startswith("."):
                # 隐藏文件（.gitkeep、.DS_Store 等）与内部临时文件不导入
                report["skipped"].append({"path": rel, "reason": "隐藏或内部文件"})
                continue
            if not os.path.isfile(path):
                report["skipped"].append({"path": rel, "reason": "非普通文件"})
                continue
            try:
                src_hash = file_sha256(path)
                stored = self.unique_stored_name(space_id, safe_stored_name(rel))
                dest = os.path.join(self.space_dir(space_id), stored)
                self._copy_verified(path, dest, src_hash)
                size = os.path.getsize(dest)
                fid = self.add_file(
                    space_id=space_id,
                    stored_name=stored,
                    display_name=safe_stored_name(rel),
                    size=size,
                    sha256=src_hash,
                    origin="import",
                    owner_visitor=None,
                    owner_grant=None,
                    created_at=os.path.getmtime(path),
                )
                report["imported"].append({"file_id": fid, "source": rel,
                                           "stored": stored, "sha256": src_hash})
            except Exception as exc:  # noqa: BLE001
                report["failed"].append({"path": rel, "error": f"{exc!r}"})
        manifest = os.path.join(self.export_dir, f"import-{int(now())}.json")
        try:
            with open(manifest, "w", encoding="utf-8") as f:
                json.dump({"space_id": space_id, "source": src_root, "report": report}, f,
                          ensure_ascii=False, indent=2)
            report["manifest"] = manifest
        except OSError:
            report["manifest"] = None
        return report

    @staticmethod
    def _copy_verified(src: str, dest: str, expect_hash: str):
        """复制并校验；校验失败时删除目标文件，避免留下未验证副本。"""
        tmp = dest + ".importing"
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout, HASH_CHUNK)
            fout.flush()
            os.fsync(fout.fileno())
        got = file_sha256(tmp)
        if got != expect_hash:
            os.remove(tmp)
            raise ValueError(f"复制结果校验失败（{got[:12]}…）")
        os.replace(tmp, dest)




