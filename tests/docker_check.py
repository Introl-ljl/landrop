"""Exercise the built image as a non-root, read-only collection service."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from landrop.remote import Client, upload_file


def main():
    image = os.environ.get("LANDROP_TEST_IMAGE", "landrop:adr-verify")
    with tempfile.TemporaryDirectory(prefix="landrop-container-") as temporary:
        root = Path(temporary)
        data, source = root / "data", root / "source"
        data.mkdir()
        source.mkdir()
        root.chmod(0o755)
        data.chmod(0o777)
        (source / "payload.txt").write_text("container native client payload", encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        name = "landrop-adr-" + str(os.getpid())
        def docker(*args, **kwargs):
            return subprocess.run(["docker", *args], check=True, capture_output=True,
                                  encoding="utf-8", timeout=60, **kwargs).stdout
        try:
            docker("run", "-d", "--rm", "--name", name, "--user", "10001:0", "--read-only",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp",
                   "-v", str(data) + ":/data", "-v", str(source) + ":/source:ro",
                   "-p", f"127.0.0.1:{port}:{port}", image, "--port", str(port),
                   "--ip", "192.168.1.104", "--collect-dir", "/data/collected")
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urllib.request.urlopen(base, timeout=2) as response:
                        assert b"LAN Drop" in response.read()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.1)
            docker("exec", name, "python", "-m", "landrop", "send", "/source/payload.txt",
                   "--upload", "--public", "--label", "container test")
            shares = json.loads(docker("exec", name, "python", "-m", "landrop", "shares", "--json"))
            share = shares[0]
            assert share["collection_dir"].startswith("/data/collected/")
            client = Client(base, str(root / "receiver"))
            client.login(share["code"])
            assert len(client.content["files"]) == 1
            upload_file(client, str(source / "payload.txt"), on_progress=lambda *_: None)
            assert len(client.refresh()["files"]) == 2
            client.submit_text("container text")
            assert len(client.refresh()["texts"]) == 1
            try:
                client.request("GET", "/admin")
            except Exception as exc:
                assert getattr(exc, "status", None) == 404
            else:
                raise AssertionError("Unexpected remote management page")
            docker("stop", "--time", "15", name)
            assert (data / "shares.sqlite3").exists()
            assert list((data / "collected").glob("*/payload.txt"))
            print("Non-root read-only container: HTTP, local management, file/text collection and retention passed")
        finally:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
