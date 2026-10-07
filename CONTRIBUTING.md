# 开发约定

- 运行时仅使用 Python 3.8+ 标准库。GUI 使用 tkinter，Pillow 只用于测试截图。
- `server/store.py` 是唯一分享状态层，`server/app.py` 是唯一 HTTP 服务；本机 GUI 与 CLI 直接管理本地状态，不增加 LAN 管理接口、管理员凭证或另一套传输引擎。
- `remote.py`、`common.py` 和 `qr.py` 不导入 GUI 或服务器，客户端协议与本机管理分离。
- 新功能先对照 `ADR.md`；不重新引入空间、一次性取件次数、完整文本编辑器或后台自启。访客删除仍待决策，重新讨论并确认前不实现。
- 用户可见行为须在桌面、CLI、网页相应入口对齐；权限、路径边界和传输中断都在服务端执行，不能仅靠隐藏按钮。
- 文件与文本列表只返回元信息，完整文本按条加载；ZIP 和媒体不整份读入内存。
- 原文件和完成收集的文件不因停用、过期、换码或删除分享而自动删除。旧状态先备份，不默默沿用旧凭证。
- 采用现有控件、颜色令牌和标准库 API，避免泛化框架、平行状态来源及为了兼容已移除能力而增加分支。

## 验证

```bash
python3 tests/smoke.py
python3 tests/cli_check.py
python3 tests/transfer_check.py
xvfb-run -a python3 tests/gui_check.py
```

`smoke.py` 使用真实 HTTP 请求验证 ADR；`transfer_check.py` 注入中断和文件/权限变化；GUI 测试驱动真实控件。Mac/Windows 直接运行 GUI 测试。

```bash
python3 packaging/build_pyz.py /tmp/landrop.pyz
python3 tests/package_check.py --cli /tmp/landrop.pyz
pyinstaller --clean --noconfirm landrop.spec
xvfb-run -a python3 tests/package_check.py --cli dist/LANDrop/landrop --gui dist/LANDrop/landrop-gui
```

打包检查通过 `LANDROP_TEST_COMMAND` 将同一 CLI 合同测试指向实际产物，不另维护一套不同的期望。`.github/workflows/ci.yml` 与 `build.yml` 保留多平台测试、构建、安装和卸载验证。
