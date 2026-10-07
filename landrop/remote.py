"""The shared browser/desktop/CLI protocol client. No server or GUI imports."""
from __future__ import annotations

import errno
import hashlib
import http.client
import http.cookiejar
import json
import os
import secrets
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from .common import (CHUNK, Cancelled, CliError, file_version, normalize_base, parse_target,
                     progress, resolve_data_dir, safe_relpath, sha256_file, unique_path)


def _save_json(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path),
                                     delete=False) as stream:
        json.dump(value, stream)
        temporary = stream.name
    os.replace(temporary, path)


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


class Client:
    def __init__(self, base, data_dir=None):
        self.base = normalize_base(base)
        self.cache_dir = os.path.join(resolve_data_dir(data_dir)[0], "receivers")
        os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
        self.cookie_file = os.path.join(self.cache_dir, hashlib.sha256(self.base.encode()).hexdigest() + ".cookies")
        self.cookies = http.cookiejar.MozillaCookieJar(self.cookie_file)
        try:
            self.cookies.load(ignore_discard=True)
        except (OSError, http.cookiejar.LoadError):
            pass
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.content = {}

    def request(self, method, path, body=None, headers=None, timeout=300, raw=False):
        headers = dict(headers or {})
        if self.content and path not in ("/api/login", "/api/logout"):
            headers.setdefault("X-LANDrop-Share", self.content["share"]["id"])
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            response = self.opener.open(urllib.request.Request(
                self.base + path, data=body, method=method, headers=headers), timeout=timeout)
        except urllib.error.HTTPError as exc:
            with exc:
                payload = exc.read()
            try:
                message = json.loads(payload).get("error", payload.decode(errors="replace"))
            except (ValueError, AttributeError):
                message = payload.decode(errors="replace")
            error = CliError(message, exc.code)
            error.headers = exc.headers
            raise error from None
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            raise CliError(f"连接中断：{exc}") from None
        if raw:
            return response
        try:
            with response:
                return json.loads(response.read() or b"{}")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise CliError(f"响应不完整：{exc}") from None

    def login(self, code):
        self.content = self.request("POST", "/api/login", {"code": code})
        with tempfile.NamedTemporaryFile(dir=self.cache_dir, delete=False) as stream:
            temporary = stream.name
        try:
            self.cookies.save(temporary, ignore_discard=True)
            os.replace(temporary, self.cookie_file)
        finally:
            _remove(temporary)
        return self.content

    def refresh(self):
        self.content = self.request("GET", "/api/files")
        return self.content

    def text(self, text_id):
        with self.request("GET", "/api/text?" + urllib.parse.urlencode({"id": text_id}), raw=True) as response:
            raw = response.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise CliError("文本超过单条上限")
        return raw.decode("utf-8")

    def submit_text(self, content):
        return self.request("POST", "/api/text", content.encode("utf-8"), {"Content-Type": "text/plain; charset=utf-8"})


def list_remote(address, code="", only=None, data_dir=None):
    base, code = parse_target(address, code)
    client = Client(base, data_dir)
    files = client.login(code)["files"]
    if only:
        files = [item for item in files if item["name"] in set(only) or item["id"] in set(only)]
        if not files:
            raise CliError("没有匹配的文件")
    return client, files


def upload_file(client, path, on_progress=progress, rel="", should_cancel=None):
    def check():
        if should_cancel and should_cancel():
            raise Cancelled("已取消")
    name = rel or os.path.basename(path)
    version, total = file_version(path), os.path.getsize(path)
    visitor = next((cookie.value for cookie in client.cookies if cookie.name == "ld_visitor"), "")
    identity = hashlib.sha256((client.base + visitor + client.content["share"]["id"] +
                               os.path.realpath(path)).encode()).hexdigest()
    cache = os.path.join(client.cache_dir, identity + ".upload.json")
    try:
        with open(cache, encoding="utf-8") as stream:
            state = json.load(stream)
    except (OSError, ValueError):
        state = {}
    if state.get("version") != version:
        check()
        state = {"id": secrets.token_hex(16), "version": version, "sha256": sha256_file(path)}
        _save_json(cache, state)
    params = {"id": state["id"], "path": name, "total": total, "sha256": state["sha256"]}
    offset = 0
    try:
        with open(path, "rb") as source:
            while True:
                check()
                if file_version(path) != version:
                    raise CliError("源文件已变化，请重新发送", 409)
                source.seek(offset)
                block = source.read(CHUNK)
                for attempt in range(4):
                    check()
                    try:
                        result = client.request("PUT", "/api/upload?" + urllib.parse.urlencode(
                            dict(params, offset=offset)), block, {"Content-Type": "application/octet-stream"})
                        break
                    except CliError as exc:
                        if exc.status == 409 and hasattr(exc, "headers") and exc.headers.get("X-Expected-Offset"):
                            offset = int(exc.headers["X-Expected-Offset"])
                            result = None
                            break
                        if attempt == 3 or (exc.status and exc.status < 500 and exc.status != 429):
                            raise
                        time.sleep(attempt + 1)
                if result is None:
                    continue
                if result.get("complete"):
                    if result["sha256"] != state["sha256"]:
                        raise CliError("服务端校验结果不一致")
                    on_progress(name, total, total)
                    _remove(cache)
                    return result
                offset = result.get("offset", offset + len(block))
                on_progress(name, offset, total)
                if result.get("publishing"):
                    time.sleep(0.2)
    except (Cancelled, KeyboardInterrupt):
        try:
            client.request("DELETE", "/api/upload?" + urllib.parse.urlencode({"id": state["id"]}))
        except CliError:
            pass
        _remove(cache)
        raise


def _publish(part, target, force):
    if force:
        os.replace(part, target)
        return target
    directory, name = os.path.dirname(target), os.path.basename(target)
    while True:
        target = unique_path(directory, name)
        try:
            os.link(part, target)
            _remove(part)
            return target
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EACCES, errno.EXDEV, errno.ENOTSUP):
                raise
        marker = target + ".landrop-pending"
        owned, marker_owned = False, False
        try:
            with open(marker, "xb"):
                marker_owned = True
            with open(target, "xb") as output, open(part, "rb") as source:
                owned = True
                shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            _remove(part)
            return target
        except FileExistsError:
            if not owned:
                continue
            raise
        except BaseException:
            if owned:
                _remove(target)
            raise
        finally:
            if marker_owned:
                _remove(marker)


def download_file(client, item, directory, force=False, on_progress=progress, should_cancel=None, _retry=True):
    directory = os.path.realpath(directory)
    relative = safe_relpath(item.get("path") or item["name"])
    target = os.path.join(directory, relative)
    parent = os.path.realpath(os.path.dirname(target))
    if os.path.commonpath((directory, parent)) != directory:
        raise CliError("保存位置包含越界链接")
    os.makedirs(parent, exist_ok=True)
    identity = hashlib.sha256((client.base + item["id"] + target).encode()).hexdigest()[:24]
    part = os.path.join(parent, ".landrop-" + identity + ".part")
    metadata = part + ".json"
    try:
        with open(metadata, encoding="utf-8") as stream:
            state = json.load(stream)
    except (OSError, ValueError):
        state = {}
    if state.get("version") != item["version"]:
        _remove(part)
    _save_json(metadata, {"version": item["version"]})
    resume = os.path.getsize(part) if os.path.exists(part) else 0
    if resume > item["size"]:
        resume = 0
    headers = {"Range": f"bytes={resume}-", "If-Range": f'"{item["version"]}"'} if resume and resume < item["size"] else {}
    if resume == item["size"]:
        resume = 0
    digest = hashlib.sha256()
    if resume:
        with open(part, "rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    params = {"id": item["id"], "version": item["version"]}
    try:
        with client.request("GET", "/api/download?" + urllib.parse.urlencode(params),
                            headers=headers, raw=True) as response:
            expected = response.headers.get("X-File-SHA256", "")
            if len(expected) != 64 or response.headers.get("ETag") != f'"{item["version"]}"':
                raise CliError("对端未提供有效的文件版本或校验信息")
            if resume and response.status != 206:
                resume, digest = 0, hashlib.sha256()
            done = resume
            with open(part, "ab" if resume else "wb") as output:
                while True:
                    if should_cancel and should_cancel():
                        raise Cancelled("已取消")
                    try:
                        block = response.read(1024 * 1024)
                    except (OSError, http.client.HTTPException):
                        break
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                    done += len(block)
                    on_progress(item["name"], done, item["size"])
                output.flush()
                os.fsync(output.fileno())
        if done != item["size"]:
            raise CliError("下载未完成，可重新接收以续传")
        if digest.hexdigest() != expected:
            _remove(part)
            if resume and _retry:
                return download_file(client, item, directory, force, on_progress, should_cancel, False)
            raise CliError("文件校验失败，未保存到目标文件")
        target = _publish(part, target, force)
        _remove(metadata)
        return target
    except (Cancelled, KeyboardInterrupt):
        _remove(part)
        _remove(metadata)
        raise


def fetch_files(address, directory, only=None, force=False, on_progress=progress, on_done=None,
                code="", data_dir=None, should_cancel=None):
    directory = os.path.abspath(os.path.expanduser(directory))
    os.makedirs(directory, exist_ok=True)
    client, files = list_remote(address, code, only, data_dir)
    if not files:
        raise CliError("当前分享没有文件，可在页面查看或提交文本")
    saved = []
    for item in files:
        target = download_file(client, item, directory, force, on_progress, should_cancel)
        saved.append(target)
        if on_done:
            on_done(item, target)
    return saved
