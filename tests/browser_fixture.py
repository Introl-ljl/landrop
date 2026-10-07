"""Isolated, non-production fixtures for real desktop/mobile browser verification."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from landrop.common import make_link
from landrop.server.app import Runtime
from landrop.server.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = Path(args.dir)
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "报告.txt").write_text("LAN Drop browser verification\n文本与文件共享。\n", encoding="utf-8")
    (source / ("这是用于验证长文件名自动换行的资料" * 3 + ".txt")).write_text("long filename", encoding="utf-8")
    shutil.copy2(ROOT / "packaging" / "assets" / "landrop.png", source / "图片.png")
    (source / "empty.txt").touch()
    with wave.open(str(source / "音频.wav"), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * 8000)
    state = Store(str(root / "state"))
    state.configure(collect_dir=str(root / "collections"))
    public = state.save_share(paths=[str(source)], label="局域网分享", allow_upload=True, public_received=True)
    private = state.save_share(paths=[str(source / "报告.txt")], label="私密收集", allow_upload=True)
    state.add_text(public["id"], "这是本机发布的文本\nhttps://example.invalid/test")
    runtime = Runtime(state)
    info = runtime.start(args.port)
    fixture = {"base": info["base"], "local_url": info["local_url"], "public": public, "private": private,
               "public_link": make_link(info["local_url"], public["code"]),
               "private_link": make_link(info["local_url"], private["code"])}
    (root / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(fixture, ensure_ascii=False), flush=True)
    try:
        while runtime.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()
        state.close()


if __name__ == "__main__":
    main()
