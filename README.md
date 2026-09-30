# LAN Drop · 局域网文件收集与快捷分享

[![CI](https://github.com/Introl-ljl/landrop/actions/workflows/ci.yml/badge.svg)](https://github.com/Introl-ljl/landrop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)
![Zero dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen)

**English summary** — A zero-dependency (Python stdlib) LAN file collector/sharer. Run the server on one
machine; others upload/download from a browser with per-link permissions (upload-only / read + delete-own /
full), SHA-256 verification and resumable uploads. Quick transfer with a **file code**:
`landrop.py send a.zip` prints a code + a command, the receiver runs `landrop.py get <code>`.
Windows/macOS get a GUI launcher, Linux gets the CLI, Docker is supported. MIT licensed.

## 下载 / Download

到 [Releases](https://github.com/Introl-ljl/landrop/releases) 下载对应平台的产物（含 `SHA256SUMS`）：

| 平台 | 文件 |
| --- | --- |
| Windows | `landrop-windows-x86_64.exe`（GUI）、`landrop-cli-windows-x86_64.exe`、`landrop-server-windows-x86_64.exe` |
| macOS (Apple Silicon / Intel) | `landrop-macos-arm64` / `landrop-macos-x86_64`（GUI），以及同名系列的 `-cli-`、`-server-` |
| Linux (x86_64 / arm64) | `landrop-cli-linux-*`、`landrop-server-linux-*`（Linux 只提供命令行） |
| Docker | `docker pull ghcr.io/introl-ljl/landrop:latest` |
| 任意有 Python 的系统 | 只下载 `landrop.py`（客户端，单文件） |

> macOS 二进制未签名：首次运行请在「系统设置 → 隐私与安全性」允许，或 `xattr -d com.apple.quarantine <文件>`。
> Windows 可能出现 SmartScreen 提示，选择「仍要运行」。

在一台电脑上启动，同一局域网内的手机与电脑用浏览器打开链接即可**投递文件、取件、按权限删除**。
文件全部落在本机（或你指定的磁盘目录），链接可随时撤销，上传完成会给出一份
**服务端实算的 SHA-256 校验值**。

它解决的场景是"**一次活动、一组人、一批文件**"：

- 让 20 个人把照片传到你的电脑上，但不让他们看到彼此的文件（仅上传链接）；
- 让自己的设备之间互传，还能删掉自己传错的（可读 + 只删自己的）；
- 给协作者一条完整读写链接，能整理所有人的文件；
- 任何一次投递都能拿到摘要，核对"最终收到的文件"和"发送出去的文件"是否一致。

- 🔑 **多链接独立授权**：每条链接有自己的权限、有效期与空间，可单独撤销、轮换。
- 🧑🤝🧑 **三档权限**：仅上传 / 可读·只删自己的 / 完整读写；权限由服务端强制，不只是隐藏按钮。
- 🗂️ **多空间隔离**：每个空间是独立目录 + 独立链接集合，互不可见；存储位置可指定。
- 🔐 **SHA-256 校验**：分块齐全后由服务端从**最终文件**重算摘要并登记；浏览器能算摘要时会自动比对。
- 📦 **任意文件与断点续传**：流式上传、8 MiB 分块、失败续传、下载支持 Range。
- 🖥️ **三种形态**：Docker Compose / 单文件可执行包 / 桌面启动器（Windows、macOS、Linux）。
- 🪶 **零第三方运行依赖**：服务端与桌面启动器都只用 Python 标准库（SQLite + http.server + tkinter）。

---

## 快速开始

### 方式一：直接运行（需要 Python 3.8+，已在 3.8 / 3.10 / 3.12 实测）

```bash
python3 server.py --data-dir ./data
```

启动后终端会打印访问地址、管理员密钥与共享目录。用管理员密钥进入 `/admin` 创建分享链接：
把链接发给同事，他们打开就能上传；你可以随时撤销或轮换这条链接。

自定义存储位置（收集到活动硬盘）：

```bash
python3 server.py --data-dir ./data --dir /mnt/disk/毕业照 --space 毕业照
```

导入已有目录里的历史文件（复制 + 逐文件校验，原目录保持不动）：

```bash
python3 server.py --data-dir ./data --import ./uploads --import-recursive
```

### 方式二：桌面启动器（Windows / macOS / Linux）

```bash
python3 desktop.py                 # 打开窗口，点「启动服务」
python3 desktop.py --autostart     # 打开窗口并立即启动（适合开机自启）
python3 desktop.py --no-window     # 不开窗口，只按环境变量启动服务
```

窗口配置也可用环境变量预填：`LANDROP_DATA_DIR`、`LANDROP_SHARE_DIR`、
`LANDROP_PORT`、`LANDROP_SCOPE`（`lan` 或 `local`）。

窗口里选择共享目录与数据目录、选择「局域网 / 仅本机」、点「启动服务」。
之后用「打开管理面板」创建链接，其余操作都在浏览器里完成。局域网内其他人**不需要安装任何东西**。

### 方式三：单文件分发包

在**目标操作系统**上执行（不能跨平台交叉编译）：

```bash
pip install pyinstaller
pyinstaller --clean --noconfirm landrop.spec
```

产物：

| 文件 | 说明 |
| --- | --- |
| `dist/landrop` | 桌面启动器 GUI（服务 / 发送 / 接收），**仅 Windows 与 macOS 生成** |
| `dist/landrop-cli` | 命令行：`send` / `get` / `revoke` / `serve`，所有平台 |
| `dist/landrop-server` | 命令行服务器，参数与 `server.py` 相同，所有平台 |

### 方式四：Docker Compose（常态化运行）

```bash
cp .env.example .env        # 设置 LANDROP_ADMIN_KEY、端口、PUID/PGID
chown -R $(id -u):$(id -g) data
docker compose up -d --build
docker compose logs | grep -E "访问地址|管理员密钥"
```

> 升级提示：v2 的数据、链接与权限都持久化在数据卷里。旧版只有 `uploads/` 一个目录，
> 请用 `--import ./uploads` 导入，**不要**直接删除旧目录。

---

## 文件码快传（Windows / macOS 用 GUI，Linux 用 CLI）

| 平台 | 形态 | 入口 |
| --- | --- | --- |
| Windows / macOS | 图形界面 | `python3 desktop.py`（或打包产物 `landrop`）→「发送文件」「接收文件」页 |
| Linux | 命令行 | `python3 landrop.py …`（或打包产物 `landrop-cli`） |

**图形界面**：先在「服务」页启动服务（发送页会自动填好地址与密钥）→「发送文件」页点「添加文件…」多选 →
「上传并生成文件码」→ 一键复制文件码 / 取件命令；对方在「接收文件」页粘贴文件码（整条命令也行）、
选保存目录、点「下载」。也可随时「撤销这个文件码」。

**命令行**（三个平台都能用，接收端只需拷走 `landrop.py`）：

不想开浏览器时，用命令行把一个或多个文件变成**文件码 + 取件命令**。仅需 Python 3.8+ 标准库，
接收端只要拷走 `landrop.py` 这一个文件。

```bash
# 发送端（服务所在机器；自动从 ./data/admin-key.txt 读管理员密钥）
python3 landrop.py send a.zip b.png "*.log"        # 一个或多个文件，支持通配符
python3 landrop.py send                            # 不带参数：列出当前目录，按编号多选
python3 landrop.py send a.zip --expire 2           # 文件码 2 小时后失效（0 = 永不过期，默认 24）
# 输出：
#   文件码:  192.168.1.5:8000/AbC…
#   取件命令: python3 landrop.py get 192.168.1.5:8000/AbC…

# 接收端：直接复制上面的命令
python3 landrop.py get 192.168.1.5:8000/AbC…                # 下载到当前目录
python3 landrop.py get 192.168.1.5:8000/AbC… -o ~/Downloads  # 指定目录（不存在会创建）
python3 landrop.py get <文件码> --list                       # 只看有哪些文件
python3 landrop.py get <文件码> --only a.zip -f              # 只取某个文件；同名覆盖（默认自动改名）

# 提前撤销（send 结束时会打印授权 ID）
python3 landrop.py revoke gs_xxxxxxxxxxxx

# Linux 直接启动服务（参数同 server.py）
python3 landrop.py serve --data-dir ./data
```

- 每次 `send` 会新建一个**独立空间**和一条**取件授权**（`可读 · 只删自己的`），文件码只能看到这一批文件。
- 上传按 8 MiB 分块并携带本地 SHA-256，服务端不一致会拒收；下载时用服务端摘要逐个校验，失败的文件不落盘。
- 发送端参数：`--server`（默认 `http://127.0.0.1:8000`，或 `LANDROP_URL`）、`--key` / `LANDROP_ADMIN_KEY`、
  `--data-dir`、`--public-url`（写入文件码的对外地址，默认取服务端探测到的局域网地址）、`--label`。
- Docker 部署：服务跑在容器里，CLI 在宿主机/其他电脑上用 `--server http://<宿主机IP>:<端口>`；镜像已内置 `landrop.py`，并在 `http://<地址>/downloads/landrop.py` 提供下载；构建好的 Linux 二进制放进 `static/downloads/` 后重新 `docker compose up -d --build` 即可一并提供（Windows 的 exe 需在 Windows 上用 `pyinstaller landrop.spec` 构建后同样放入）。
- Windows 控制台自动切 UTF-8；下载文件名会清洗 Windows 非法字符与保留名（CON、NUL…）。
- 目录暂不支持，请先打包；文件码是明文 HTTP 下的凭证，仅限可信局域网内使用。

---

## 权限模型

管理员持有**管理员密钥**，可以创建任意多条**分享链接**；每条链接独立授权：

| 权限档 | 能看到已有文件 | 下载 | 删除 | 说明 |
| --- | --- | --- | --- | --- |
| `仅上传` | ✗ | ✗ | ✗ | 投递箱：上传后只拿到自己的回执，看不到空间内容 |
| `可读 · 只删自己的` | ✓ | ✓ | 仅自己上传的 | 适合互相取件、但各自负责自己的文件 |
| `完整读写` | ✓ | ✓ | 全部 | 适合协作者整理同一批文件 |

要点：

- **权限在服务端强制**。伪造请求、直接调 API 都会按同一套规则被拒（见 `tests/smoke.py`）。
- **"自己的文件"= 同一浏览器 + 同一条链接上传的文件**。这是匿名浏览器身份，不是实名账号：
  同一条链接换浏览器或清空 Cookie 后，无法再证明旧文件属于自己；需要跨设备找回时，
  请每位参与者使用**不同的链接**，或让管理员代为整理。
- 撤销或轮换链接后，已打开的页面**立即**失去权限，不需要等会话过期。
- 管理员密钥与分享链接是两层：分享链接永远拿不到管理能力。

---

## 校验（SHA-256）

1. 上传的最后一个分块到达后，服务端把文件**原子落盘**，然后从**最终文件**流式重算 SHA-256
   （1 MiB 分块读取，不整份载入内存）。
2. 如果客户端提供了 `sha256`（浏览器在安全上下文或大文件受限时可能没有），服务端会比对：
   **不一致就拒绝发布**，返回 `422`，删除分块，文件不会出现在列表里。
3. 回执与文件列表都带上摘要；页面上的勾选按钮可随时对**磁盘现状**再算一次并比对登记值。
4. 摘要是**校验值**，用于确认"收到的字节与发出的字节一致"，它不加密文件、也不是密码。

浏览器在 `http://` 局域网地址下没有 `crypto.subtle`（非安全上下文），此时由服务端计算摘要；
页面会明确标注"（与本地一致）"只在两端都算过时才显示。

---

## 数据与目录布局

`--data-dir` 下：

```
data/
├── state.sqlite3      # 空间、授权、访客、会话、文件索引、分块状态
├── admin-key.txt      # 自动生成的管理员密钥（0600，便于找回）
├── files/<空间>/       # 已完成的文件本体（可用 --dir 或每个空间单独指定位置）
├── partial/           # 上传中的分块（不属于任何共享目录，不会出现在列表里）
├── trash/<空间>/       # 删除的文件，便于人工恢复
└── exports/           # 导入清单等导出物
```

- 元数据、分块、文件**都必须放在持久卷里**：Compose 挂载整个数据目录，只挂 `uploads/` 会丢链接与摘要。
- 文件 API 只按不可变文件 ID 访问 `files/`，数据库与分块永远不可被列出或下载。

---

## 配置项

### 命令行参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--data-dir` | `./data` | 数据目录（状态库、文件、分块、回收站） |
| `--dir` | 空 | 把默认空间的共享目录放到指定位置（仅首次创建时生效） |
| `--space` | `共享空间` | 默认空间名称 |
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8000` | 监听端口 |
| `--admin-key` | 随机生成 | 首次启动使用的管理员密钥，也可用环境变量 `LANDROP_ADMIN_KEY` |
| `--reset-admin` | 关闭 | 配合 `--admin-key` 重置管理员密钥（旧密钥立即失效） |
| `--import` | 空 | 导入已有目录到默认空间（复制 + 校验，源目录不动） |
| `--import-recursive` | 关闭 | 导入时递归子目录 |
| `--open-browser` | 关闭 | 启动后自动打开浏览器 |
| `--public-url` | 自动探测 | 分享链接里使用的对外地址；容器内必须设为宿主机局域网地址 |
| `--no-banner` | 关闭 | 不打印启动横幅（GUI 调用时使用） |

`desktop.py` 另有 `--autostart`（开窗后立即启动）与 `--no-window`（仅按环境变量启动服务）。

### `.env`（Docker Compose）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LANDROP_ADMIN_KEY` | 空（随机） | 首次启动的管理员密钥；留空则随机生成并写入数据目录 |
| `LANDROP_PORT` | `8000` | 宿主机端口 |
| `LANDROP_PUBLIC_URL` | 空（自动探测） | 分享链接用的对外地址，如 `http://192.168.1.104:8800`；容器内自动探测到的是 Docker 内网地址，必须显式设置 |
| `LANDROP_DATA_DIR` | `./data` | 宿主机数据目录（状态库与文件都在这里） |
| `PUID` / `PGID` | `1000` | 容器运行身份，需与数据目录属主一致 |
| `TZ` | `Asia/Shanghai` | 时区 |

---

## HTTP API

除首页、`/admin` 与静态资源外，其余接口都需要登录（`HttpOnly` Cookie 会话）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/login` | `{"key": "..."}` 密钥或口令；返回权限与空间 |
| `POST` | `/api/logout` | 退出登录 |
| `GET` | `/api/me` | 当前会话与能力（未登录返回 `logged_in:false`） |
| `GET` | `/api/files[?space=]` | 列出有权查看的文件（仅上传档返回空列表） |
| `PUT` | `/api/upload?name=&id=&offset=&total=&sha256=` | 流式分块上传；`sha256` 可选但建议提供 |
| `DELETE` | `/api/upload/abort?id=` | 放弃未完成的上传并清理分块 |
| `GET` | `/api/download?id=` | 下载，支持 `Range` 与 `X-File-SHA256` 响应头 |
| `GET` | `/api/verify?id=` | 从磁盘重算摘要并与登记值比对 |
| `DELETE` | `/api/delete?id=` | 删除（按权限档判定，管理员可加 `&space=`） |
| `GET` / `POST` | `/api/grants` | 管理员：列出 / 创建授权 |
| `POST` | `/api/grants/revoke` | 管理员：撤销授权（`{"id": "..."}`） |
| `POST` | `/api/grants/rotate` | 管理员：轮换密钥并返回新明文 |
| `GET` / `POST` | `/api/spaces` | 管理员：列出 / 创建空间 |

管理员操作非自己空间的文件时，加 `&space=<空间ID>`（如 `/api/files?space=sp_xxx`）。

---

## 安全设计

- **服务端裁决权限**：`visitor`（浏览器匿名主体）+ `grant`（授权）+ `space`（空间）三元组共同决定
  能否上传续传、下载、删除；客户端传来的 `owner` 之类字段一律不采信。
- **密钥存储**：链接密钥只存 SHA-256；人类口令用 PBKDF2-SHA256（随机盐、12 万轮）存储。
- **会话绑定授权版本**：撤销/轮换会提升版本号，使旧会话立即失效。
- **分隔的 Cookie**：会话与访客身份都放在 `HttpOnly; SameSite=Lax` Cookie 中；访客 secret 仅存摘要。
- **防爆破**：同一 IP 登录失败限速，失败响应固定延迟；`hmac.compare_digest` 定时安全比较。
- **跨站写请求**：带 `Origin` 的写请求必须同源（配合 `SameSite` Cookie 阻断 CSRF）。
- **路径安全**：文件按不可变 ID 与登记文件名访问，上传文件名会被清洗；元数据与分块不在共享目录内。
- **删除可恢复**：默认移入 `trash/`，而不是直接抹掉。
- **Docker 沙盒**：只读根文件系统、`cap_drop: ALL`、`no-new-privileges`、资源上限、仅挂载数据目录。

> ⚠️ 本工具面向**可信局域网**，使用明文 HTTP，请勿直接暴露到公网。
> 需要公网或敏感数据时，请自行在前面加 TLS 反向代理，并重新评估信任边界。
> 桌面形态没有容器隔离，局域网内任何能连上端口的人都可以尝试登录。

---

## 测试

```bash
python3 tests/smoke.py          # 98 项端到端断言：权限矩阵、校验、续传、撤销、空间隔离
bash tests/binary_check.sh      # 对分发包做同样的登录/上传/摘要/重启验证
```

`smoke.py` 会真起一个服务、真发 HTTP 请求，覆盖：三档权限的正反用例、跨访客与跨链接的越权尝试、
同名文件互不覆盖、错误摘要必须 `422` 且不留文件、断点续传的身份绑定、撤销与轮换的即时生效、
管理员跨空间操作等。

平台验证边界：上述自动化验证在 Linux 上完成（服务端、GUI 启动器、PyInstaller 单文件产物、Docker 镜像均已实际运行）；Windows 与 macOS 的产物需要在对应系统上各自构建后验证，本项目未做跨平台交叉构建。

---

## 目录结构

```
.
├── server.py            # HTTP 服务与权限判定（标准库）
├── store.py             # SQLite 持久状态层：空间/授权/访客/会话/文件索引/分块
├── desktop.py           # 跨平台桌面启动器（tkinter）
├── landrop.py           # 命令行与核心：send / get / revoke / serve（Linux 主入口）
├── desktop_share.py     # 桌面启动器的「发送 / 接收」页（Windows / macOS GUI）
├── static/
│   ├── index.html       # 访客单页（上传、取件、校验）
│   └── admin.html       # 管理面板（空间与链接管理）
├── tests/
│   ├── smoke.py         # 端到端权限与校验测试
│   └── binary_check.sh  # 分发包验证脚本
├── landrop.spec         # PyInstaller 打包配置
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## 常见问题

**Q：管理员密钥忘了？**
A：看启动日志，或读数据目录里的 `admin-key.txt`；也可以用 `--reset-admin --admin-key '新密钥'` 重置。

**Q：链接发出去后想收回？**
A：管理面板里点「撤销」，已打开的页面立即失效；只想换密钥不影响权限档位时用「轮换密钥」。

**Q：对方说看不到任何文件？**
A：那条链接的权限是「仅上传」，这是设计行为；需要互相取件就建「可读·只删自己的」链接。

**Q：收到的文件和发送的不一致怎么办？**
A：用文件行上的校验按钮或 `/api/verify` 重新计算并与回执摘要比对；上传时提供摘要的话，
不一致的文件根本不会被发布。

**Q：链接的密钥忘了 / 没保存？**
A：链接密钥只以摘要形式存储，无法找回明文。到管理面板点「轮换密钥」会生成一条新链接，旧密钥立即失效。

**Q：手机上能传吗？**
A：可以。手机与电脑连同一局域网，浏览器打开链接即可，不需要安装 App。

**Q：删除的文件去哪了？**
A：`data/trash/<空间>/` 下，确认不需要后再自行清理。

---

## 参与贡献 / License

欢迎 issue 与 PR，开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。以 [MIT](LICENSE) 协议开源。
