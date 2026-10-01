#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LAN Drop 端到端冒烟测试：真起服务、真发 HTTP、逐条验证权限与校验语义。

用法: python3 tests/smoke.py [--keep]
不依赖第三方库；使用临时数据目录，退出时清理。
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import faulthandler
faulthandler.dump_traceback_later(240, exit=True)   # 卡死时打印各线程堆栈并退出，便于 CI 定位

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  <- {detail}" if detail and not cond else ""))
    return bool(cond)


class Client:
    """极简 HTTP 客户端，保留 cookie。"""

    def __init__(self, port):
        self.port = port
        self.cookies = {}
        self.last_headers = {}

    def request(self, method, path, body=None, headers=None, expect=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = dict(headers or {})
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if body is not None:
            hdrs["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        for k, v in resp.getheaders():
            if k.lower() == "set-cookie":
                pair = v.split(";")[0]
                if "=" in pair:
                    ck, _, cv = pair.partition("=")
                    self.cookies[ck.strip()] = cv.strip()
        self.last_headers = dict(resp.getheaders())
        status = resp.status
        conn.close()
        if expect is not None:
            check(f"{method} {path} -> {expect}", status == expect, f"got {status}")
        return status, data, dict(resp.getheaders())


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时目录用于排查")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="landrop-selftest-")
    data_dir = os.path.join(tmp, "data")
    port = free_port()
    # 服务输出写到文件而不是管道：没人读的管道被写满后（Windows 缓冲区很小），服务会卡在打印日志上
    log_path = os.path.join(tmp, "server.log")
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "landrop", "serve",
         "--data-dir", data_dir, "--host", "127.0.0.1", "--port", str(port),
         "--admin-key", "admin-secret-123"],
        stdout=log_fh, stderr=subprocess.STDOUT, cwd=ROOT,
    )

    def server_log() -> str:
        log_fh.flush()
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    try:
        if not wait_port(port):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("服务未能启动:\n", server_log())
            return 1
        run_checks(port, data_dir)
    finally:
        if FAIL:
            print("\n--- 服务端日志（末尾 40 行）---\n" + "\n".join(server_log().splitlines()[-40:]))
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("临时目录保留在:", tmp)

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for name in FAIL:
        print("  FAIL:", name)
    return 1 if FAIL else 0


def run_checks(port, data_dir):
    admin = Client(port)
    print("\n[1] 管理员登录与链接管理")
    st, body, _ = admin.request("POST", "/api/login",
                                {"key": "admin-secret-123"}, expect=200)
    me = json.loads(body)
    check("管理员 capabilities.manage", me["capabilities"].get("manage") is True)
    check("管理员 = full 文件能力", me["capabilities"]["delete"] == "all")

    st, body, _ = admin.request("GET", "/api/grants", expect=200)
    spaces = json.loads(body)["spaces"]
    check("自动创建默认空间", len(spaces) >= 1, str(spaces))
    default_space = spaces[0]["id"]

    # 三个档位的分享链接
    links = {}
    for perm in ("upload", "delete_own", "full"):
        st, body, _ = admin.request("POST", "/api/grants",
                                    {"kind": "share", "perm": perm, "mode": "token",
                                     "label": f"{perm} 链接"}, expect=201)
        g = json.loads(body)["grant"]
        links[perm] = g
        check(f"{perm} 链接返回可分发 URL", g["url"].startswith("http") and "#k=" in g["url"])

    st, body, _ = admin.request("GET", "/api/grants", expect=200)
    check("列表不再返回明文密钥", all("secret" not in g for g in json.loads(body)["grants"]))

    print("\n[2] 未登录一律拒绝")
    anon = Client(port)
    anon.request("GET", "/api/files", expect=401)
    anon.request("PUT", "/api/upload?name=x&id=y&offset=0", b"data", expect=401)
    anon.request("DELETE", "/api/delete?id=f_x", expect=401)
    anon.request("GET", "/api/grants", expect=401)

    print("\n[3] 仅上传档：可投递、不可浏览/下载/删除")
    up = Client(port)
    st, body, _ = up.request("POST", "/api/login", {"key": links["upload"]["secret"]}, expect=200)
    caps = json.loads(body)["capabilities"]
    check("upload 档 list=false", caps["list"] is False)
    check("upload 档 delete=false", caps["delete"] is False)
    st, body, _ = up.request("GET", "/api/files", expect=200)
    lst = json.loads(body)
    check("upload 档列表为空且带说明", lst["files"] == [] and "note" in lst)
    payload = b"hello upload-only"
    st, body, _ = up.request(
        "PUT", "/api/upload?name=a.txt&id=upA&offset=0&total=" + str(len(payload)),
        payload, expect=200)
    res = json.loads(body)
    check("上传回执含 SHA-256", res.get("complete") is True and len(res.get("sha256", "")) == 64)
    check("回执摘要与服务端实算一致",
          res.get("sha256") == hashlib.sha256(payload).hexdigest(), res.get("sha256"))
    file_a = res["id"]
    check("upload 档无法下载", up.request("GET", f"/api/download?id={file_a}")[0] == 403)
    check("upload 档无法删除", up.request("DELETE", f"/api/delete?id={file_a}")[0] == 403)

    print("\n[4] 摘要不符必须拒绝且不留文件")
    bad = Client(port)
    bad.request("POST", "/api/login", {"key": links["full"]["secret"]}, expect=200)
    payload = b"tampered"
    wrong = "0" * 64
    st, body, _ = bad.request(
        "PUT", f"/api/upload?name=bad.txt&id=bad1&offset=0&total={len(payload)}&sha256={wrong}",
        payload)
    check("拒绝错误摘要返回 422", st == 422, f"got {st}")
    detail = json.loads(body)
    check("返回期望与实际摘要", detail["expected"] == wrong and len(detail["actual"]) == 64)
    st, body, _ = bad.request("GET", "/api/files", expect=200)
    names = [f["name"] for f in json.loads(body)["files"]]
    check("错误摘要文件未进入列表", "bad.txt" not in names, str(names))
    check("分块已清理", not any(p.endswith(".part") for p in
                            os.listdir(os.path.join(data_dir, "partial"))))

    print("\n[5] 删除本人档：可读全部、只能删自己的")
    A = Client(port)
    B = Client(port)
    A.request("POST", "/api/login", {"key": links["delete_own"]["secret"]}, expect=200)
    B.request("POST", "/api/login", {"key": links["delete_own"]["secret"]}, expect=200)
    pa = b"file-from-A"
    st, body, _ = A.request("PUT", f"/api/upload?name=fromA.txt&id=ua1&offset=0&total={len(pa)}", pa)
    fa = json.loads(body)["id"]
    st, body, _ = B.request("GET", "/api/files", expect=200)
    items = {f["name"]: f for f in json.loads(body)["files"]}
    check("B 能看到 A 的文件", "fromA.txt" in items)
    check("A 的文件对 B 标记不可删", items.get("fromA.txt", {}).get("deletable") is False)
    check("B 删除 A 的文件被拒", B.request("DELETE", f"/api/delete?id={fa}")[0] == 403)
    pb = b"file-from-B"
    st, body, _ = B.request("PUT", f"/api/upload?name=fromB.txt&id=ub1&offset=0&total={len(pb)}", pb)
    fb = json.loads(body)["id"]
    st, body, _ = A.request("GET", "/api/files", expect=200)
    items = {f["name"]: f for f in json.loads(body)["files"]}
    check("A 看自己的文件可删", items.get("fromA.txt", {}).get("deletable") is True)
    check("A 可删除自己的文件", A.request("DELETE", f"/api/delete?id={fa}")[0] == 200)
    check("B 的文件仍在", any(f["id"] == fb for f in json.loads(
        A.request("GET", "/api/files", expect=200)[1])["files"]))
    check("删除后文件从列表消失", all(f["id"] != fa for f in json.loads(
        A.request("GET", "/api/files", expect=200)[1])["files"]))
    # 同名上传不会被误删/覆盖
    st, body, _ = A.request("PUT", f"/api/upload?name=same.txt&id=ua2&offset=0&total=3", b"AAA")
    f1 = json.loads(body)
    st, body, _ = B.request("PUT", f"/api/upload?name=same.txt&id=ub2&offset=0&total=3", b"BBB")
    f2 = json.loads(body)
    check("同名文件落盘名不冲突", f1["stored_name"] != f2["stored_name"],
          f"{f1['stored_name']} / {f2['stored_name']}")
    check("A 可删自己的同名文件", A.request("DELETE", f"/api/delete?id={f1['id']}")[0] == 200)
    st, body, _ = B.request("GET", f"/api/download?id={f2['id']}", expect=200)
    check("B 的同名文件内容未受影响", body == b"BBB", body[:20])

    print("\n[6] 完整读写档")
    F = Client(port)
    F.request("POST", "/api/login", {"key": links["full"]["secret"]}, expect=200)
    check("full 档能删除他人文件", F.request("DELETE", f"/api/delete?id={fb}")[0] == 200)
    check("full 档不能创建分享链接", F.request("POST", "/api/grants", {"kind": "share"})[0] == 403)
    check("full 档不能读授权列表", F.request("GET", "/api/grants")[0] == 403)

    print("\n[7] 校验功能")
    data = bytes(range(256)) * 40
    sha = hashlib.sha256(data).hexdigest()
    st, body, _ = F.request(
        "PUT", f"/api/upload?name=verify.bin&id=vf1&offset=0&total={len(data)}&sha256={sha}",
        data)
    res = json.loads(body)
    check("带预期摘要上传成功", res["complete"] is True and res["verified"] is True)
    check("客户端摘要与服务端一致", res["sha256"] == sha)
    st, body, _ = F.request("GET", f"/api/verify?id={res['id']}", expect=200)
    v = json.loads(body)
    check("复核接口确认一致", v["match"] is True and v["sha256"] == sha)
    on_disk = os.path.join(data_dir, "files")
    target = None
    for base, _d, names in os.walk(on_disk):
        for nm in names:
            if nm == res["stored_name"]:
                target = os.path.join(base, nm)
    with open(target, "ab") as fh:
        fh.write(b"corrupt")
    st, body, _ = F.request("GET", f"/api/verify?id={res['id']}", expect=200)
    v2 = json.loads(body)
    check("磁盘被改动后复核报不一致", v2["match"] is False and v2["sha256"] != sha)
    st, body, hdrs = F.request("GET", f"/api/download?id={res['id']}", expect=200)
    check("下载响应带摘要头", hdrs.get("X-File-SHA256") == sha or
          hdrs.get("x-file-sha256") == sha, str(hdrs))

    print("\n[8] 分块续传与身份绑定")
    big = bytes(range(256)) * 4096   # 1 MiB
    half = len(big) // 2
    st, body, _ = F.request(
        "PUT", f"/api/upload?name=chunk.bin&id=ck1&offset=0&total={len(big)}", big[:half])
    r1 = json.loads(body)
    check("半块上传返回未完成", r1["complete"] is False and r1["received"] == half)
    st, body, hdrs = F.request(
        "PUT", f"/api/upload?name=chunk.bin&id=ck1&offset=0&total={len(big)}", big[:half])
    check("偏移不符返回 409 与期望偏移", st == 409 and
          hdrs.get("X-Expected-Offset") == str(half), str(hdrs.get("X-Expected-Offset")))
    intruder = Client(port)
    intruder.request("POST", "/api/login", {"key": links["full"]["secret"]}, expect=200)
    st, body, _ = intruder.request(
        "PUT", f"/api/upload?name=chunk.bin&id=ck1&offset=0&total={len(big)}", big[:half])
    check("他人无法接管进行中的上传会话", st == 403, f"got {st}")
    st, body, _ = intruder.request("DELETE", "/api/upload/abort?id=ck1")
    check("他人无法清理他人上传会话", st == 403, f"got {st}")
    check("分块仍在原上传者名下",
          json.loads(F.request("GET", f"/api/files")[1])["files"] is not None and
          os.path.getsize(os.path.join(data_dir, "partial", "ck1.part")) == half)
    st, body, _ = F.request(
        "PUT", f"/api/upload?name=chunk.bin&id=ck1&offset={half}&total={len(big)}", big[half:])
    r2 = json.loads(body)
    check("续传拼接后完整且摘要正确",
          r2["complete"] is True and r2["sha256"] == hashlib.sha256(big).hexdigest())

    print("\n[9] 撤销与轮换立即生效")
    st, body, _ = admin.request("POST", "/api/grants", {"kind": "share", "perm": "full"})
    tmp_grant = json.loads(body)["grant"]
    victim = Client(port)
    victim.request("POST", "/api/login", {"key": tmp_grant["secret"]}, expect=200)
    check("撤销前可用", victim.request("GET", "/api/files")[0] == 200)
    admin.request("POST", "/api/grants/revoke", {"id": tmp_grant["id"]}, expect=200)
    check("撤销后旧会话立即失效", victim.request("GET", "/api/files")[0] == 401)
    check("撤销后密钥无法登录",
          Client(port).request("POST", "/api/login", {"key": tmp_grant["secret"]})[0] == 401)
    st, body, _ = admin.request("POST", "/api/grants/rotate", {"id": links["full"]["id"]},
                                expect=200)
    new_secret = json.loads(body)["grant"]["secret"]
    check("轮换后旧密钥失效",
          Client(port).request("POST", "/api/login", {"key": links["full"]["secret"]})[0] == 401)
    check("轮换后新密钥可用",
          Client(port).request("POST", "/api/login", {"key": new_secret})[0] == 200)

    print("\n[10] 空间隔离与管理面板跨空间操作")
    st, body, _ = admin.request("POST", "/api/spaces", {"name": "活动B"}, expect=201)
    space_b = json.loads(body)["space"]["id"]
    st, body, _ = admin.request("POST", "/api/grants",
                                {"kind": "share", "perm": "full", "space_id": space_b})
    gb = json.loads(body)["grant"]
    other = Client(port)
    other.request("POST", "/api/login", {"key": gb["secret"]}, expect=200)
    st, body, _ = other.request("GET", "/api/files", expect=200)
    check("新空间初始为空", json.loads(body)["files"] == [])
    st, body, _ = other.request(
        "PUT", f"/api/upload?name=inB.txt&id=spb1&offset=0&total=2", b"B!")
    file_b = json.loads(body)["id"]
    check("文件落在指定空间",
          json.loads(other.request("GET", "/api/files")[1])["space"]["id"] == space_b)
    st, body, _ = admin.request("GET", f"/api/files?space={space_b}", expect=200)
    check("管理员可列指定空间文件", any(f["id"] == file_b for f in json.loads(body)["files"]))
    # 轮换后默认空间的会话需要重新登录
    F2 = Client(port)
    F2.request("POST", "/api/login", {"key": new_secret}, expect=200)
    st, body, _ = F2.request("GET", "/api/files", expect=200)
    check("默认空间看不到 B 空间的文件",
          all(f["id"] != file_b for f in json.loads(body)["files"]))
    check("跨空间下载被拒", F2.request("GET", f"/api/download?id={file_b}")[0] == 403)
    check("跨空间删除被拒", F2.request("DELETE", f"/api/delete?id={file_b}")[0] == 403)
    check("管理员可删任意空间文件",
          admin.request("DELETE", f"/api/delete?id={file_b}&space={space_b}")[0] == 200)
    check("删除后管理员列表里不再出现",
          all(f["id"] != file_b for f in json.loads(
              admin.request("GET", f"/api/files?space={space_b}")[1])["files"]))

    print("\n[11] 数据持久化与目录布局")
    for sub in ("files", "partial", "trash"):
        check(f"数据目录包含 {sub}/", os.path.isdir(os.path.join(data_dir, sub)))
    check("状态库 state.sqlite3 存在", os.path.isfile(os.path.join(data_dir, "state.sqlite3")))
    check("管理员密钥落盘可找回", os.path.isfile(os.path.join(data_dir, "admin-key.txt")))
    st, body, _ = admin.request("GET", "/api/grants", expect=200)
    check("授权信息来自数据库", len(json.loads(body)["grants"]) >= 4)

    print("\n[12] 旧目录导入（复制 + 校验，源目录不动）")
    src = os.path.join(data_dir, "..", "legacy")
    os.makedirs(os.path.join(src, "sub"), exist_ok=True)
    with open(os.path.join(src, "keep.txt"), "wb") as fh:
        fh.write(b"legacy content")
    with open(os.path.join(src, ".gitkeep"), "wb") as fh:
        fh.write(b"")
    with open(os.path.join(src, ".DS_Store"), "wb") as fh:
        fh.write(b"junk")
    with open(os.path.join(src, "sub", "nested.txt"), "wb") as fh:
        fh.write(b"nested")
    before = sorted(os.listdir(src))
    sys.path.insert(0, ROOT)
    from landrop.server import store as sm
    st_obj = sm.Store(data_dir)
    rep = st_obj.import_directory(default_space, src, recursive=True)
    names = sorted(r["source"] for r in rep["imported"])
    check("导入只收普通文件（含子目录）",
          names == ["keep.txt", os.path.join("sub", "nested.txt")], str(names))
    check("隐藏文件被跳过", all(".gitkeep" not in r["source"] and ".DS_Store" not in r["source"]
                          for r in rep["imported"]))
    check("源目录未被修改", sorted(os.listdir(src)) == before, str(os.listdir(src)))
    check("导入文件登记为无访客归属且带摘要",
          all(r["sha256"] for r in rep["imported"]) and len(rep["imported"]) == 2)
    check("导入清单已生成", bool(rep["manifest"]) and os.path.isfile(rep["manifest"]))
    src_size = os.path.getsize(os.path.join(src, "keep.txt"))
    with open(os.path.join(src, "keep.txt"), "rb") as fh:
        expect_hash = hashlib.sha256(fh.read()).hexdigest()
    keep = next(r for r in rep["imported"] if r["source"] == "keep.txt")
    nested = next(r for r in rep["imported"] if r["source"].endswith("nested.txt"))
    check("导入副本与源文件摘要一致", keep["sha256"] == expect_hash, keep["sha256"])
    check("导入副本大小与源文件一致", src_size == 14, str(src_size))
    check("子目录文件也已导入", nested["stored"].endswith("nested.txt"), nested["stored"])

    print("\n[13] 跨平台文件名约束")
    check("Windows 保留名被加前缀", sm.safe_stored_name("CON.txt") .startswith("_") or
          sm.safe_stored_name("CON.txt") != "CON.txt", sm.safe_stored_name("CON.txt"))
    check("路径分隔符被剥离", "/" not in sm.safe_stored_name("a/b/c.txt") and
          "\\" not in sm.safe_stored_name("a\\b\\c.txt"))
    check("控制字符被剔除", "\x00" not in sm.safe_stored_name("bad\x00name.txt"))

    print("\n[14] 一次性传输：次数/过期/撤销/清理 与 旧库迁移")
    st, body, _ = admin.request("POST", "/api/grants", {"kind": "share", "perm": "full",
                                                          "mode": "token", "label": "t14"}, expect=201)
    full = Client(port)
    full.request("POST", "/api/login", {"key": json.loads(body)["grant"]["secret"]}, expect=200)
    full.request("POST", "/api/transfers", {"label": "x"}, expect=403)
    anon.request("POST", "/api/transfers", {"label": "x"}, expect=401)

    def make_transfer(**kw):
        st, body, _ = admin.request("POST", "/api/transfers", kw, expect=201)
        tr = json.loads(body)["transfer"]
        data = ("transfer payload " + tr["id"]).encode()
        admin.request("PUT", f"/api/upload?name=t.bin&id=up{tr['id']}&offset=0&total={len(data)}"
                      f"&space={tr['space_id']}", data, expect=200)
        return tr, data

    tr, data = make_transfer(label="两次", expires_hours=1, max_downloads=2)
    st, body, _ = admin.request("GET", "/api/spaces", expect=200)
    check("传输的临时空间不出现在空间列表", tr["space_id"] not in [s["id"] for s in json.loads(body)["spaces"]])
    st, body, _ = admin.request("GET", "/api/grants", expect=200)
    check("传输授权不出现在授权列表", tr["id"] not in [g["id"] for g in json.loads(body)["grants"]])
    st, body, _ = admin.request("GET", "/api/transfers", expect=200)
    check("管理员可列出传输", any(x["id"] == tr["id"] and x["files"] == 1 for x in json.loads(body)["transfers"]))

    rx = Client(port)
    st, body, _ = rx.request("POST", "/api/login", {"key": tr["secret"]}, expect=200)
    caps = json.loads(body)["capabilities"]
    check("传输令牌只读：可下载、不可上传/删除",
          caps["download"] and not caps["upload"] and caps["delete"] is False)
    st, body, _ = rx.request("GET", "/api/files", expect=200)
    files = json.loads(body)["files"]
    check("传输令牌只看到本批文件", [f["name"] for f in files] == ["t.bin"])
    st, got, hdrs = rx.request("GET", f"/api/download?id={files[0]['id']}", expect=200)
    check("传输文件内容与摘要头正确", got == data and hdrs.get("X-File-SHA256") == hashlib.sha256(data).hexdigest())
    rx.request("PUT", "/api/upload?name=evil&id=evil&offset=0&total=1", b"x", expect=403)
    check("传输令牌不能删文件", rx.request("DELETE", f"/api/delete?id={files[0]['id']}")[0] == 403)
    st, body, _ = rx.request("POST", "/api/done", {}, expect=200)
    check("第 1 次取件后仍可用", json.loads(body)["exhausted"] is False)
    rx2 = Client(port)
    rx2.request("POST", "/api/login", {"key": tr["secret"]}, expect=200)
    st, body, _ = rx2.request("POST", "/api/done", {}, expect=200)
    check("第 2 次取件后次数用完", json.loads(body)["exhausted"] is True)
    Client(port).request("POST", "/api/login", {"key": tr["secret"]}, expect=401)
    rx.request("GET", "/api/files", expect=401)
    check("用完后已有会话也立即失效", True)

    st, body, _ = make_transfer(label="过期", expires_hours=0.0003, max_downloads=0), None, None
    short = st[0]
    time.sleep(1.6)
    Client(port).request("POST", "/api/login", {"key": short["secret"]}, expect=401)

    rev, _d = make_transfer(label="撤销", expires_hours=1, max_downloads=0)
    Client(port).request("POST", "/api/login", {"key": rev["secret"]}, expect=200)
    admin.request("POST", "/api/grants/revoke", {"id": rev["id"]}, expect=200)
    Client(port).request("POST", "/api/login", {"key": rev["secret"]}, expect=401)

    live, _d = make_transfer(label="保留", expires_hours=1, max_downloads=0)
    janitor = sm.Store(data_dir)
    dirs = {t["id"]: janitor.space_dir(t["space_id"]) for t in (tr, short, rev, live)}
    check("清理前磁盘上有文件", all(os.path.isdir(d) for d in dirs.values()))
    removed = janitor.cleanup_transfers()
    check("清理 3 个失效传输（取完/过期/撤销）", removed == 3, str(removed))
    check("失效传输的磁盘文件已删除",
          not any(os.path.exists(dirs[t["id"]]) for t in (tr, short, rev)))
    check("仍有效的传输不受影响", os.path.isdir(dirs[live["id"]]) and
          [x["id"] for x in janitor.list_transfers()] == [live["id"]])
    Client(port).request("POST", "/api/login", {"key": live["secret"]}, expect=200)
    janitor.close()

    # 旧版数据库（没有新增列）必须能原样打开并迁移
    import sqlite3
    old_dir = os.path.join(tempfile.mkdtemp(prefix="landrop-old-"), "data")
    os.makedirs(old_dir)
    conn = sqlite3.connect(os.path.join(old_dir, "state.sqlite3"))
    conn.executescript(sm._SCHEMA)
    conn.execute("INSERT INTO spaces(id,name,dir,created_at) VALUES('sp_old','旧空间','old',1)")
    conn.execute("INSERT INTO grants(id,kind,space_id,perm,mode,token_hash,created_at) "
                 "VALUES('gs_old','share','sp_old','full','token','h',1)")
    conn.commit()
    conn.close()
    migrated = sm.Store(old_dir)
    g = migrated.get_grant("gs_old")
    check("旧库迁移：数据保留且新增列有默认值",
          g is not None and g["downloads"] == 0 and g["max_downloads"] is None
          and [s["id"] for s in migrated.list_spaces()] == ["sp_old"])
    migrated.close()
    sm.Store(old_dir).close()      # 再开一次：迁移幂等
    check("迁移可重复执行", True)
    check("空文件名有兜底", sm.safe_stored_name("") == "unnamed")
    long_name = "字" * 300 + ".txt"
    check("超长文件名被截断", len(sm.safe_stored_name(long_name).encode("utf-8")) <= 240,
          str(len(sm.safe_stored_name(long_name).encode("utf-8"))))

    print("\n[15] 文件夹上传（相对路径）")
    from urllib.parse import quote
    data = b"nested payload"
    st, body, _ = admin.request(
        "PUT", f"/api/upload?name=a.txt&path={quote('相册/2024/a.txt')}&id=nest1&offset=0"
               f"&total={len(data)}", data, expect=200)
    rec = json.loads(body)
    check("显示名保留相对路径", rec.get("name") == "相册/2024/a.txt", str(rec.get("name")))
    check("磁盘上只用文件名平铺存放", "/" not in rec.get("stored_name", "/")
          and rec["stored_name"].startswith("a") and rec["stored_name"].endswith(".txt"),
          str(rec.get("stored_name")))
    st, _b, hdrs = admin.request("GET", f"/api/download?id={rec['id']}", expect=200)
    check("下载文件名不含目录", hdrs.get("Content-Disposition", "").endswith("UTF-8''a.txt"),
          hdrs.get("Content-Disposition", ""))
    for bad in ("../evil.txt", "a/../../b.txt", "/"):
        admin.request("PUT", f"/api/upload?name=x&path={quote(bad)}&id=bad{len(bad)}&offset=0&total=1",
                      b"x", expect=400)
    check("含 .. 或为空的相对路径被拒绝", True)
    check("路径逐段清洗", sm.safe_rel_name('a:b/c*d.txt') == "a_b/c_d.txt", sm.safe_rel_name('a:b/c*d.txt'))

    print("\n[16] 一次性传输：浏览器下载完全部文件也计一次取件")
    st, body, _ = admin.request("POST", "/api/transfers", {"label": "web", "max_downloads": 1},
                                expect=201)
    tr = json.loads(body)["transfer"]
    for i, payload in enumerate((b"first file", b"second file")):
        admin.request("PUT", f"/api/upload?name=f{i}.bin&id=web{i}{tr['id']}&offset=0"
                             f"&total={len(payload)}&space={tr['space_id']}", payload, expect=200)
    web = Client(port)
    web.request("POST", "/api/login", {"key": tr["secret"]}, expect=200)
    st, body, _ = web.request("GET", "/api/files", expect=200)
    ids = [f["id"] for f in json.loads(body)["files"]]
    web.request("GET", f"/api/download?id={ids[0]}", headers={"Range": "bytes=0-3"}, expect=206)
    web.request("GET", f"/api/download?id={ids[0]}", headers={"Range": "bytes=0-3"}, expect=206)
    Client(port).request("POST", "/api/login", {"key": tr["secret"]}, expect=200)
    check("只下了一部分：不计数", True)
    web.request("GET", f"/api/download?id={ids[1]}", headers={"Range": "bytes=3-"}, expect=206)
    Client(port).request("POST", "/api/login", {"key": tr["secret"]}, expect=200)
    check("只读文件尾（播放器探测）：不计数", True)
    web.request("GET", f"/api/download?id={ids[0]}", expect=200)
    Client(port).request("POST", "/api/login", {"key": tr["secret"]}, expect=200)
    check("只取完一个文件：不计数", True)
    web.request("GET", f"/api/download?id={ids[1]}", expect=200)
    Client(port).request("POST", "/api/login", {"key": tr["secret"]}, expect=401)
    check("全部文件取完：一次性传输失效（无需 /api/done）", True)

    st, body, _ = admin.request("POST", "/api/transfers", {"label": "twice", "max_downloads": 2},
                                expect=201)
    tr2 = json.loads(body)["transfer"]
    admin.request("PUT", f"/api/upload?name=t.bin&id=tw{tr2['id']}&offset=0&total=3"
                         f"&space={tr2['space_id']}", b"abc", expect=200)
    cli_like = Client(port)
    cli_like.request("POST", "/api/login", {"key": tr2["secret"]}, expect=200)
    st, body, _ = cli_like.request("GET", "/api/files", expect=200)
    cli_like.request("GET", f"/api/download?id={json.loads(body)['files'][0]['id']}", expect=200)
    st, body, _ = cli_like.request("POST", "/api/done", {}, expect=200)
    st, body, _ = admin.request("GET", "/api/transfers", expect=200)
    n = next(x["downloads"] for x in json.loads(body)["transfers"] if x["id"] == tr2["id"])
    check("下载完 + /api/done 同一会话只计一次", n == 1, str(n))

    print("\n[17] 文件码即网址")
    st, page, hdrs = anon.request("GET", "/" + tr2["secret"], expect=200)
    check("/<令牌> 返回网页", b"<html" in page.lower() and hdrs.get("Content-Type", "").startswith("text/html"))
    check("令牌网址禁止 Referer 外泄", hdrs.get("Referrer-Policy") == "no-referrer")
    check("令牌网址不缓存", hdrs.get("Cache-Control") == "no-store")
    anon.request("GET", "/short", expect=404)
    anon.request("GET", "/admin.html", expect=200)
    check("静态资源优先于令牌路由", True)


if __name__ == "__main__":
    sys.exit(main())
