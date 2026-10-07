# LAN Drop

局域网文件互传与收集，运行时仅依赖 Python 标准库。桌面窗口和 CLI 共用一个本地服务、同一份本机状态。

## 使用模型

- 选择几个文件或整个文件夹，创建一份分享，自动生成六位数字授权码。
- 接收者访问发送设备的 `IP:端口`，输入授权码；扫码或打开便捷链接可以免输入。
- 每份分享可独立开启上传、公开收到的内容、指定收集目录及有效期。
- 文件夹动态共享，新增文件自动加入；单独选文件不会共享其父目录。
- 收集文件按任务自动创建子目录，同名文件自动改名，不覆盖已有文件。
- 简单文本可以查看、复制和提交，权限与文件收集共用。不包含文本编辑器或聊天系统。

没有中心中转、管理员密钥、远程管理面板、独立空间或取件次数限制。访客删除暂不提供，保留在 [ADR.md](ADR.md) 中待后续决策。

## 运行

```bash
python3 -m landrop gui
python3 -m landrop --help
```

也可以 `pip install .`，之后使用 `landrop` 和 `landrop-gui`。Python 3.8+；桌面需要 tkinter，Linux 无桌面环境仍可运行 CLI。

桌面只有三个页面：

| 页面 | 内容 |
| --- | --- |
| 分享 | 创建、编辑和启停分享，选择文件、发布文本、配置收集、换码和二维码；统一启动或停止本机服务 |
| 接收 | 连接对方地址和授权码，选择文件接收，查看文本，向允许上传的分享投递内容；保留最近 5 次接收记录 |
| 设置 | 默认目录、外观、网络与预览上限、命令行 PATH 和本机诊断日志 |

打开窗口不自动启动服务。创建新分享是主动发布操作，会启动服务；关闭窗口停止本机服务，进行中的任务会先提示确认。最小化可以继续运行。

## CLI

以下命令也可通过 `python3 -m landrop` 或 `python3 landrop.pyz` 运行。

```bash
# 在本机创建分享；没有已启动的服务时持续运行，Ctrl-C 只停止服务
landrop send a.zip photos/
landrop send --upload --label 活动照片
landrop send --upload --public --label 共同收集 --dir ~/Pictures
landrop send --text '一段文本或链接'
landrop send --text-file note.txt

# 默认端口 8000，可先在本机设置中修改
landrop settings --port 8800 --ip 192.168.1.104
landrop serve
landrop stop

# 接收：地址与六位码，或者便捷链接；默认保存到当前工作目录
landrop get 192.168.1.104:8800 --code 038251 -o ~/Downloads
landrop get 'http://192.168.1.104:8800/#code=038251' --list
landrop get 192.168.1.104:8800 --code 038251 --only photos/a.jpg

# 投递到允许上传的分享；普通授权码不授予设备管理权限
landrop put 192.168.1.104:8800 a.zip --code 038251
landrop put 192.168.1.104:8800 --code 038251 --text '回复文本'

# 本机管理：ID 来自 shares 列表，不通过 LAN 管理接口
landrop shares
landrop shares --json
landrop shares <ID> --disable
landrop shares <ID> --enable
landrop shares <ID> --rotate
landrop shares <ID> --expire 24
landrop shares <ID> --files new.txt another-folder/
landrop shares <ID> --no-upload --public
landrop shares <ID> --clear-text --yes
landrop shares <ID> --delete --yes

# 上限以 MiB 为单位；超出预览上限仍可下载完整文件
landrop settings --text-preview-mib 10 --image-preview-mib 20
```

`--data-dir` 或 `LANDROP_DATA_DIR` 指定本机状态目录。窗口与 CLI 使用相同目录才能管理同一组分享；服务正在运行时，新的 `send` 只创建分享，不启动第二个服务。

## 文件、文本与权限

分享直接读取原文件，不额外复制。源文件移动或删除后不可下载；传输中变化会中止，断点只允许同一版本续传。

“允许上传”默认关闭，同时控制文件上传与文本提交。“公开收到的内容”也默认关闭，控制其他持码者能否查看收到的文件和文本。本机主动选择或发布的内容则属于该分享的公开内容。

每份分享最多 100 条文本，单条最多 **10 MiB**。文本随分享保存在本机，重启保留。默认文本预览 **10 MiB**、图片预览 **20 MiB**，可在高级设置调整；音视频使用浏览器原生流式播放，其他格式直接下载。浏览器批量下载使用 ZIP，不在页面中缓存整个压缩包。

停用可以恢复原码，换码和删除则永久废弃旧码。旧码不分配给其他分享，旧链接和会话无法访问另一份分享。停用、到期或换码会中止当前传输，已传出的字节无法收回。

关闭或到期不删除文件、目录和文本。删除分享需要确认，会删除配置及关联文本，**不删除磁盘文件**。实际磁盘删除使用系统文件管理器。中断上传保留 24 小时供续传，明确取消清理临时数据，完整文件不受影响。

整目录共享与私密收集目录重叠时需要本机确认，因为另一份目录分享可能公开当前及后续文件；原收集码仍遵守自己的公开选项。不要把不准备共享的资料放入已经共享的目录。

## 数据与升级

默认状态目录：Windows `%APPDATA%\LANDrop`，macOS `~/Library/Application Support/LANDrop`，Linux `~/.local/share/landrop`。Windows zip 免安装版带 `portable.txt` 时使用程序目录下的 `data/`。

```text
data/
  shares.sqlite3    当前分享、授权码保留表、会话、上传登记、文本和设置
  service.lock      本机服务单实例锁
  partial/          未完成上传，不进入共享列表
  receivers/        本机客户端会话与续传状态，不记录到接收历史
  diagnostics.log   本机诊断日志
  legacy-backup/    首次升级时的旧状态、配置及旧密钥备份
```

默认收集目录为系统下载目录下的 `LANDrop`，每个任务自动创建子目录。修改任务保存位置只影响后续上传，不搬动旧文件。

新模型使用 `shares.sqlite3`。发现旧 `state.sqlite3` 时先做一致性备份，保留旧数据库、文件和目录，不复用旧凭证或链接；按新模型重新创建分享。旧 `send --server`、管理员参数、一次性文件码和 `gui --autostart` 不再支持。

## Docker 收集

Docker 只是本地收集服务的部署方式，不是中转服务器。

```bash
cp .env.example .env
# 设置宿主机 LAN 地址 LANDROP_IP，及目录属主对应的 PUID/PGID
mkdir -p data
docker compose up -d --build
docker compose exec landrop python -m landrop send --upload --label 收集
docker compose exec landrop python -m landrop shares
docker compose down
```

容器内默认收集到 `/data/collected`。整个 `/data` 持久挂载；根文件系统只读、无额外 capabilities，默认不随宿主机重启自动启动。宿主机目录须能被配置的 PUID/PGID 写入。映射端口与服务端口一致，`LANDROP_IP` 用于生成对其他设备可达的链接。

## 构建与验证

安装版及免安装版的 Windows/macOS/Linux 打包脚本保留；一个应用目录共用 GUI 和 CLI 运行时，Windows 单文件免安装版只有 GUI。当前源码的发布由标签工作流触发，未发布的改动不会自动进入已有 Release。

```bash
python3 tests/smoke.py
python3 tests/cli_check.py
python3 tests/transfer_check.py
xvfb-run -a python3 tests/gui_check.py

python3 packaging/build_pyz.py
pyinstaller --clean --noconfirm landrop.spec
xvfb-run -a python3 tests/package_check.py --cli dist/LANDrop/landrop --gui dist/LANDrop/landrop-gui
python3 packaging/package.py
```

macOS/Windows 的 GUI 测试直接运行，无需 `xvfb-run`。打包检查复用 CLI 合同测试，覆盖文件夹、中文文件名、重复接收、收集、文本及静态资源。

## 安全边界

仅用于可信局域网，默认是**未加密 HTTP**，授权码和便捷链接都是访问凭证。不要直接暴露公网，也不要认为六位码或 SHA-256 等同于加密。

连续错误登录按来源暂时限速，不锁死分享或关闭服务。权限始终由服务端校验，管理操作只在本机窗口/CLI 中完成。自动校验保留，普通界面不展示摘要或手动校验按钮；浏览器缺少客户端摘要时，仅能证明服务端已计算收到的内容，不能声称已做双端一致性校验。

设计记录见 [ADR.md](ADR.md)，开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。MIT License。
