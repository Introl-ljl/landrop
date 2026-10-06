# Contributing / 参与贡献

Thanks for helping! / 欢迎提 issue 和 PR。

## Ground rules / 约定
- **Zero third-party runtime dependencies** — server, CLI and desktop launcher use the Python
  standard library only (Python 3.8+). 运行时不引入第三方依赖。
- Permissions are enforced **server-side**; any new endpoint needs a positive and a negative case in
  `tests/smoke.py`. 权限必须在服务端裁决，新接口请补正反用例。
- `landrop/common.py`, `direct.py`, `remote.py` and `qr.py` (the client side) must not import from `landrop/server` or
  `landrop/gui`, so the client stays usable on its own (`landrop.pyz`). 客户端部分不得依赖服务端/GUI。
- **One app, two entry points.** The desktop window and the `landrop` CLI ship together in every package and must
  stay feature-equivalent: a new transfer option goes into both (`cli.py` and `gui/panels.py`), backed by the same
  function in `direct.py` / `remote.py`. 窗口与命令行是同一个程序的两个入口，功能要对齐。
- GUI colors come from `landrop/gui/theme.py`, which mirrors the web UI tokens in `server/static/index.html`;
  change both together. 桌面与网页共用一套配色。

## Dev setup / 本地开发
```bash
python3 -m landrop serve --data-dir ./data   # run the server
python3 tests/smoke.py                       # server: permissions, checksums, resume, transfers, migration
python3 tests/cli_check.py                   # CLI: direct send/get, via-server send, revoke
xvfb-run -a python3 tests/gui_check.py       # GUI (Linux needs xvfb; macOS/Windows run it directly)
python3 -m landrop gui                       # desktop window (needs tkinter)

pip install pyinstaller                      # packaging, on the target OS
pyinstaller --clean --noconfirm landrop.spec # dist/LANDrop: window + CLI sharing one runtime
python3 packaging/package.py                 # dist/release: setup + portable for this platform
python3 tests/package_check.py --cli dist/LANDrop/landrop --gui dist/LANDrop/landrop-gui
```
CI runs the tests on Linux, macOS and Windows, then builds, installs, checks and uninstalls the packages on every
platform (`.github/workflows/build.yml`). Windows installers need Inno Setup 6.5+ (`choco install innosetup`).

## Releases / 发布
Bump `landrop/__init__.py`, push a tag `vX.Y.Z`; the `release` workflow builds the setup + portable packages
for Windows / macOS (arm64, x86_64) / Linux (x86_64, arm64) plus `landrop.pyz`, publishes a GitHub Release with
`SHA256SUMS`, and pushes a Docker image to GHCR. Running the workflow manually only builds and verifies.
