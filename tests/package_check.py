"""Reuse the CLI contract suite against installed binaries or a zipapp, then test the GUI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def command(value):
    path = shutil.which(value) if not Path(value).is_file() else str(Path(value).resolve())
    if not path:
        raise RuntimeError("Executable not found: " + value)
    return [sys.executable, path] if path.endswith(".pyz") else [path]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli")
    parser.add_argument("--gui")
    parser.add_argument("--expect-data-dir")
    args = parser.parse_args()
    if not args.cli and not args.gui:
        parser.error("--cli or --gui is required")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("LANDROP_DATA_DIR", None)
    with tempfile.TemporaryDirectory(prefix="landrop-package-") as temporary:
        if args.cli:
            cli = command(args.cli)
            subprocess.run([*cli, "--version"], env=env, check=True, timeout=30)
            tests = dict(env, LANDROP_TEST_COMMAND=json.dumps(cli))
            subprocess.run([sys.executable, str(ROOT / "tests" / "cli_check.py")],
                           env=tests, cwd=temporary, check=True, timeout=240)
            if args.expect_data_dir:
                subprocess.run([*cli, "settings"], env=env, cwd=temporary, check=True, timeout=30,
                               stdout=subprocess.DEVNULL)
                if not (Path(args.expect_data_dir) / "shares.sqlite3").is_file():
                    raise RuntimeError("Portable data directory did not match the package contract")
        if args.gui:
            gui = command(args.gui)
            data = Path(temporary) / "gui-state"
            subprocess.run([*gui, "--smoke-test"], env=dict(env, LANDROP_DATA_DIR=str(data)),
                           check=True, timeout=120)
            if not (data / "shares.sqlite3").is_file():
                raise RuntimeError("GUI did not use its isolated local state directory")
    print("Package contracts passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
