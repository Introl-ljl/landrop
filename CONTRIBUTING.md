# Contributing / 参与贡献

Thanks for helping! / 欢迎提 issue 和 PR。

## Ground rules / 约定
- **Zero third-party runtime dependencies** — server, CLI and desktop launcher use the Python
  standard library only (Python 3.8+). 运行时不引入第三方依赖。
- Permissions are enforced **server-side**; any new endpoint needs a positive and a negative case in
  `tests/smoke.py`. 权限必须在服务端裁决，新接口请补正反用例。
- `landrop/common.py`, `direct.py` and `remote.py` (the client side) must not import from `landrop/server` or
  `landrop/gui`, so the client stays usable on its own (`landrop.pyz`). 客户端部分不得依赖服务端/GUI。

## Dev setup / 本地开发
```bash
python3 -m landrop serve --data-dir ./data   # run the server
python3 tests/smoke.py                       # server: permissions, checksums, resume, transfers, migration
python3 tests/cli_check.py                   # CLI: direct send/get, via-server send, revoke
xvfb-run -a python3 tests/gui_check.py       # GUI (Linux needs xvfb; macOS/Windows run it directly)
python3 -m landrop gui                       # GUI launcher (needs tkinter)
```
CI runs these on Linux, macOS and Windows.

## Releases / 发布
Push a tag `vX.Y.Z`; the `release` workflow builds binaries on all platforms, publishes a GitHub
Release with `SHA256SUMS`, and pushes a Docker image to GHCR.
