"""客户端：连接常驻服务或直连发送端，完成上传 / 下载 / 传输创建。仅标准库。"""
from __future__ import annotations

import hashlib
import http.client
import http.cookiejar
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

from .common import (CliError, CHUNK, sha256_file, cmd_prefix, collect_entries, default_data_dir,
                     make_code, normalize_base, parse_code, progress, safe_relpath, unique_path)


class Client:
    def __init__(self, base: str):
        self.base = normalize_base(base)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method, path, body=None, headers=None, timeout=60, raw=False):
        data = None
        hdrs = dict(headers or {})
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        elif body is not None:
            data = body
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            resp = self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                msg = json.loads(payload).get("error") or payload.decode(errors="replace")
            except Exception:
                msg = payload.decode(errors="replace")
            err = CliError(f"{method} {path.split('?')[0]} → {exc.code} {msg}".strip())
            err.status = exc.code  # type: ignore[attr-defined]
            err.headers = exc.headers  # type: ignore[attr-defined]
            raise err from None
        except urllib.error.URLError as exc:
            raise CliError(f"连接不上 {self.base}：{exc.reason}") from None
        except (OSError, http.client.HTTPException) as exc:     # 连接被重置 / 对端已关闭等
            raise CliError(f"与 {self.base} 的连接中断：{exc}") from None
        if raw:
            return resp
        with resp:
            payload = resp.read()
        return json.loads(payload) if payload else {}

    def login(self, secret: str) -> dict:
        return self.request("POST", "/api/login", {"key": secret})


def read_admin_key(data_dir: str | None = None) -> str:
    """依次尝试：指定目录 → LANDROP_DATA_DIR → ./data → 平台默认数据目录。"""
    dirs = [data_dir, os.environ.get("LANDROP_DATA_DIR"), "./data", default_data_dir()]
    for d in filter(None, dirs):
        try:
            with open(os.path.join(d, "admin-key.txt"), encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
        except OSError:
            continue
    raise CliError("找不到管理员密钥：请用 --key、环境变量 LANDROP_ADMIN_KEY，"
                   "或 --data-dir 指向含 admin-key.txt 的数据目录")


def upload_file(cli: Client, space_id: str, path: str, on_progress=progress, rel: str = ""):
    """上传一个文件；rel 是文件夹里的相对路径（如 ``相册/a.jpg``），服务端据此保留目录结构。"""
    name = os.path.basename(path)
    total = os.path.getsize(path)
    digest = sha256_file(path)
    upload_id = secrets.token_hex(8)
    offset = 0
    params = {"name": name, "id": upload_id, "total": total, "sha256": digest, "space": space_id}
    if rel and rel != name:
        params["path"] = rel
        name = rel
    q = lambda off: "/api/upload?" + urllib.parse.urlencode(dict(params, offset=off))  # noqa: E731
    with open(path, "rb") as f:
        while True:
            f.seek(offset)
            block = f.read(CHUNK)
            try:
                res = cli.request("PUT", q(offset), block,
                                  {"Content-Type": "application/octet-stream"}, timeout=300)
            except CliError as exc:
                if getattr(exc, "status", None) == 409:   # 断点位置不一致：按服务端说的续传
                    expected = exc.headers.get("X-Expected-Offset")  # type: ignore[attr-defined]
                    if expected is not None and int(expected) != offset:
                        offset = int(expected)
                        continue
                raise
            offset += len(block)
            on_progress(name, min(offset, total), total)
            if res.get("complete"):
                if res.get("sha256") != digest:
                    raise CliError(f"{name}：服务端摘要与本地不一致")
                return res
            if not block and total:
                raise CliError(f"{name}：上传提前结束")


def send_files(server: str, key: str, paths: list[str], expire_hours: float = 24,
               label: str = "", public_url: str = "", on_progress=progress,
               max_downloads: int = 1) -> dict:
    """经常驻服务的一次性传输：建传输 → 上传 → 给出文件码。返回 {code, grant_id, ...}。

    传输到期 / 取够次数 / 被撤销后，服务端会自动连文件一起清理，不会留下空间与授权。
    """
    entries = collect_entries(paths)
    if not entries:
        raise CliError("没有可发送的文件")
    cli = Client(server)
    cli.login(key)
    label = (label or ", ".join(os.path.basename(p.rstrip("/\\")) for p in paths))[:60]
    tr = cli.request("POST", "/api/transfers", {
        "label": label, "expires_hours": expire_hours, "max_downloads": max_downloads})["transfer"]
    try:
        for full, rel in entries:
            upload_file(cli, tr["space_id"], full, on_progress, rel)
    except BaseException:
        try:                                    # 半途失败：撤销，交给服务端清理
            cli.request("POST", "/api/grants/revoke", {"id": tr["id"]})
        except CliError:
            pass
        raise
    code = make_code(normalize_base(public_url or _public_url(cli) or cli.base), tr["secret"])
    return {
        "code": code, "grant_id": tr["id"], "count": len(entries),
        "bytes": sum(os.path.getsize(full) for full, _ in entries), "expire_hours": expire_hours,
        "max_downloads": max_downloads, "command": f"{cmd_prefix()} get {code}",
    }


def _public_url(cli: Client) -> str:
    """服务端知道自己的对外地址（可能是局域网 IP），比 127.0.0.1 更适合发给别人。"""
    try:
        return cli.request("GET", "/api/spaces").get("public_url") or ""
    except CliError:
        return ""


def download_file(cli: Client, item: dict, directory: str, force: bool,
                  on_progress=progress, _retry=True) -> str:
    rel = safe_relpath(item.get("path") or item["name"])
    name = os.path.basename(rel)
    target = os.path.join(directory, rel) if force else unique_path(directory, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = target + ".part"

    resume = os.path.getsize(part) if os.path.exists(part) else 0
    size = int(item.get("size") or 0)
    if resume and resume >= size:          # 残留分块不可信：从头来
        resume = 0
    headers = {"Range": f"bytes={resume}-"} if resume else {}
    resp = cli.request("GET", "/api/download?" + urllib.parse.urlencode({"id": item["id"]}),
                       headers=headers, timeout=300, raw=True)
    if resume and resp.status != 206:      # 对端不支持续传
        resume = 0
    expect = (resp.headers.get("X-File-SHA256") or item.get("sha256") or "").lower()
    total = size or (int(resp.headers.get("Content-Length") or 0) + resume)
    h = hashlib.sha256()
    if resume:                              # 续传：先把已有部分并入摘要
        with open(part, "rb") as old:
            for block in iter(lambda: old.read(1024 * 1024), b""):
                h.update(block)
    done = resume
    with resp, open(part, "ab" if resume else "wb") as f:
        while True:
            try:
                block = resp.read(1024 * 1024)
            except (OSError, http.client.HTTPException):
                break                       # 连接中断：按「下载不完整」处理，保留 .part 便于续传
            if not block:
                break
            f.write(block)
            h.update(block)
            done += len(block)
            on_progress(name, done, total)
    # 网络中断：保留 .part，下次 get 会从断点继续
    if total and done != total:
        raise CliError(f"{name}：下载不完整（{done}/{total}），重新运行 get 可续传")
    if expect and h.hexdigest() != expect:
        try:
            os.remove(part)
        except OSError:
            pass
        if resume and _retry:               # 续传拼出的内容不对：整份重下一次
            return download_file(cli, item, directory, force, on_progress, _retry=False)
        raise CliError(f"{name}：SHA-256 校验失败，已丢弃")
    os.replace(part, target)
    return target


def list_remote(code: str, only=None) -> tuple[Client, list[dict]]:
    base, token = parse_code(code)
    cli = Client(base)
    cli.login(token)
    files = cli.request("GET", "/api/files").get("files", [])
    if only:
        wanted = set(only)
        files = [f for f in files if f["name"] in wanted]
    if not files:
        raise CliError("没有可下载的文件（文件码已过期、被撤销或没有匹配）")
    return cli, files


def fetch_files(code: str, directory: str, only=None, force: bool = False,
                on_progress=progress, on_done=None) -> list[str]:
    """按文件码把文件下载到 directory，逐个校验 SHA-256；返回落盘路径列表。"""
    cli, files = list_remote(code, only)
    directory = os.path.abspath(os.path.expanduser(directory or "."))
    os.makedirs(directory, exist_ok=True)
    saved = []
    for item in files:
        target = download_file(cli, item, directory, force, on_progress)
        saved.append(target)
        if on_done:
            on_done(item, target)
    try:                                    # 直连发送端据此结束；中心化服务没有该接口，忽略
        cli.request("POST", "/api/done", {})
    except CliError:
        pass
    return saved
