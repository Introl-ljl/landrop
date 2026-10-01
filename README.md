# LAN Drop · 局域网文件互传与收集

[![CI](https://github.com/Introl-ljl/landrop/actions/workflows/ci.yml/badge.svg)](https://github.com/Introl-ljl/landrop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)
![Zero dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen)

**English summary** — Zero-dependency (Python stdlib) LAN file tool with three ways to use it:
**(1)** direct peer-to-peer transfer with a file code — `landrop send a.zip` prints a code, the receiver runs
`landrop get <code>`; no server needed; **(2)** `landrop serve` turns your machine into a collection server
(web UI + CLI, per-link permissions, SHA-256 verification, resumable uploads); **(3)** run that server long-term
(Docker) and use it as a central hub with one-shot, auto-expiring transfers. One app per platform
(Windows / macOS / Linux) with a desktop window **and** the `landrop` command, shipped as an installer and a
portable build. A file code is also a URL, so phones can receive with just a browser (or by scanning a QR code). MIT.

## 你想做什么？

| 场景 | 用法 | 一句话 |
| --- | --- | --- |
| 把文件发给**另一台电脑**，用完即走 | [① 直连文件码](#-直连文件码互传去中心化) | `landrop send a.zip` → 对方 `landrop get <文件码>`，不需要服务 |
| 让**一群人**往我电脑上传文件（浏览器即可） | [② 收集服务](#-收集服务landrop-serve) | `landrop serve`，把链接发给大家 |
| 常驻一台机器做**中心化**中转/收集 | [③ 常驻服务](#-常驻服务docker--中心化传输) | Docker 跑服务，`landrop send --server …` 生成一次性取件码 |

## 下载 / 安装

到 [Releases](https://github.com/Introl-ljl/landrop/releases) 下载（含 `SHA256SUMS`）。每个平台都有**安装版**和**免安装版**，
两者都是同一个程序：**桌面窗口 + `landrop` 命令行**共用一份运行时，不再有单独的命令行安装包。

| 平台 | 安装版 | 免安装版 |
| --- | --- | --- |
| Windows x64 | `LANDrop-<版本>-windows-x86_64-setup.exe` | `LANDrop-<版本>-windows-x86_64-portable.zip` |
| macOS Apple 芯片 | `LANDrop-<版本>-macos-arm64-setup.pkg` | `LANDrop-<版本>-macos-arm64-portable.zip` |
| macOS Intel | `LANDrop-<版本>-macos-x86_64-setup.pkg` | `LANDrop-<版本>-macos-x86_64-portable.zip` |
| Linux x64 / arm64 | `LANDrop-<版本>-linux-<架构>-setup.deb` | `LANDrop-<版本>-linux-<架构>-portable.tar.gz` |

| 形态 | 装了什么 | 命令行在哪 | 数据在哪 |
| --- | --- | --- | --- |
| Windows 安装版 | 开始菜单「LAN Drop」「LAN Drop 命令行」；默认装到当前用户，不需要管理员 | 勾选「添加到 PATH」（默认勾选）后任意终端可用 `landrop`；卸载时自动移除 | `%APPDATA%\LANDrop` |
| macOS 安装版 | `/Applications/LAN Drop.app` | 安装时自动链接 `/usr/local/bin/landrop` | `~/Library/Application Support/LANDrop` |
| Linux 安装版（.deb） | `/opt/landrop`，应用菜单里的「LAN Drop」 | `/usr/bin/landrop`（窗口：`landrop-gui`） | `~/.local/share/landrop` |
| 免安装版（Win / Linux） | 解压即用：`LAN Drop.exe` / `landrop-gui` 与 `landrop(.exe)` 在同一目录 | 直接运行目录里的 `landrop`，或在窗口「设置 → 命令行工具」一键加入 PATH | **程序目录下的 `data/`**，整个文件夹可拷走（删掉 `portable.txt` 则改用系统目录） |
| 免安装版（macOS） | `LAN Drop.app`，放在任意位置运行 | `LAN Drop.app/Contents/MacOS/landrop`，或「设置 → 命令行工具」一键链接 | 同安装版（`.app` 内部不可写） |
| 任意装了 Python 3.8+ 的系统 | `landrop.pyz` 单文件 | `python3 landrop.pyz send …` | 同上表各平台默认目录 |
| Docker | `docker pull ghcr.io/introl-ljl/landrop:latest`（收集服务） | — | 挂载的 `/data` |
| 从源码 | `git clone …` 后 `python3 -m landrop`，或 `pip install .` 得到 `landrop` 与 `landrop-gui` | | |

> 二进制未签名：macOS 首次打开请右键「打开」，或在「系统设置 → 隐私与安全性」允许；Windows 可能出现 SmartScreen，
> 选择「更多信息 → 仍要运行」。静默安装：`setup.exe /VERYSILENT /CURRENTUSER /TASKS=addtopath`。
> 下文命令里的 `landrop` 在不同形态下等价于 `python3 -m landrop`（源码）、`python3 landrop.pyz`（pyz）。

### 桌面窗口

左侧导航四个页面，浅色 / 深色跟随系统，配色与网页一致：

- **发送**：选择（Windows 上也可以直接拖入）文件或文件夹 → 选「直连」或「经服务器」→ 生成文件码。
  结果页同时给出**文件码**、**取件命令**、**浏览器网址**和**二维码**：装了 LAN Drop 的电脑粘贴文件码，
  手机扫码就能在浏览器里下载。直连可设置等待时长、接收者数量、写进文件码的网卡地址（多网卡 / VPN）。
- **接收**：粘贴文件码（剪贴板里有文件码时自动填入）→ 可以「先看看有哪些文件」再勾选 → 接收；
  逐个校验 SHA-256，中断后重新接收自动续传，显示速度与最近接收记录。
- **收集服务**：一个开关启动 / 停止；显示访问地址、二维码、管理员密钥；「打开管理面板」免输入密钥直接登录；可看服务日志。
- **设置**：外观、命令行工具（加入 / 移出 PATH）、数据目录、版本信息。

---

## ① 直连文件码互传（去中心化）

发送端在本机临时监听一个端口，把「地址 + 令牌」编进**文件码**；接收端用文件码直接向发送端取件。
**没有服务器、没有账号**，取完（或超时 / Ctrl-C）发送端自动退出，文件码随即失效。

```bash
# 发送端
landrop send a.zip b.png photos/        # 多个文件、整个文件夹都行，可用通配符
landrop send                            # 不带参数：列出当前目录按编号多选
# 输出：
#   文件码:  192.168.1.5:41234/Xk3…
#   取件命令: landrop get 192.168.1.5:41234/Xk3…
#   等待接收端连接… 一次性，30 分钟内无人取件将自动退出；Ctrl-C 取消。

# 接收端（直接复制上面的命令）
landrop get 192.168.1.5:41234/Xk3…                # 保存到当前目录
landrop get <文件码> -o ~/Downloads               # 指定目录（不存在会创建）
landrop get <文件码> --list                       # 只看有哪些文件
landrop get <文件码> --only a.zip -f              # 只取某个文件；同名覆盖（默认自动改名）
```

- **校验与续传**：发送端先算 SHA-256，接收端逐个校验，失败不落盘；下载中断后重新运行 `get` 会从 `.part` 断点继续。
- **文件夹**：保留目录结构；接收端逐段清洗路径，拒绝 `..`。
- **选项**：`--receivers N`（允许 N 个接收者取完后退出）、`--timeout 分钟`、`--port`、`--ip`（多网卡/VPN 时指定写进文件码的地址）、
  `--qr`（在终端里打印二维码）。
- **文件码就是网址**：`http://<文件码>` 用浏览器打开即可看到文件列表并下载（多个文件可「全部下载」为 zip），
  适合手机或没装 LAN Drop 的电脑；浏览器把文件都下载完也算一次取件，发送端随之结束。
- **桌面窗口**：「发送」页添加文件/文件夹 → 生成文件码并一键复制或扫码；对方在「接收」页粘贴文件码（整条命令也行）即可。
  发送期间保持窗口打开。
- **注意**：传输是明文 HTTP，仅限可信局域网；首次监听系统防火墙会弹窗，请选「允许访问」。

---

## ② 收集服务（`landrop serve`）

在本机启动服务，同一局域网的手机与电脑用浏览器打开链接即可**投递文件、取件、按权限删除**。
适合「一次活动、一组人、一批文件」：

- 让 20 个人把照片传到你的电脑上，但互相看不到彼此的文件（仅上传链接）；
- 自己的设备之间互传，还能删掉传错的；
- 给协作者一条完整读写链接，整理所有人的文件；
- 每次投递都拿到服务端实算的 SHA-256 回执，核对「收到的」与「发出的」是否一致。

```bash
landrop serve                                         # 启动，终端打印访问地址与管理员密钥
landrop serve --dir /mnt/disk/毕业照 --space 毕业照      # 收集到指定磁盘目录
landrop serve --import ./uploads --import-recursive   # 导入历史文件（复制+校验）
landrop serve --data-dir ./data                       # 指定数据目录（默认与桌面窗口相同，见「下载 / 安装」）
landrop gui                                           # 或者打开窗口 →「收集服务」→ 打开开关
```

用管理员密钥打开 `/admin` 创建分享链接，把链接发给大家；可随时撤销或轮换（桌面窗口里点「打开管理面板」会自动登录）。
命令行与窗口**共用同一个数据目录**，所以在哪边启动，链接、文件和管理员密钥都是同一套。

**同样支持命令行操作**（网页能做的核心动作 CLI 都能做）：

```bash
landrop send a.zip --server http://192.168.1.5:8000 --key <管理员密钥>   # 上传并生成一次性取件码
landrop get  <文件码> -o ./out                                          # 用取件码取件
landrop revoke <授权ID> --server … --key …                              # 撤销
```

`--key` 也可用环境变量 `LANDROP_ADMIN_KEY`；本机运行时会自动读取数据目录里的 `admin-key.txt`。
文件夹也可以经服务器发送（保留目录结构）。经服务器生成的文件码同样可以直接用浏览器打开取件。

---

## ③ 常驻服务（Docker / 中心化传输）

把同一个服务长期跑在一台机器上，作为团队的中转站：大家都往它上传 / 从它取件，发送端不必一直在线。

```bash
cp .env.example .env        # 设置 LANDROP_ADMIN_KEY、端口、PUID/PGID、LANDROP_PUBLIC_URL
chown -R $(id -u):$(id -g) data
docker compose up -d --build
docker compose logs | grep -E "访问地址|管理员密钥"
```

- **一次性传输**：`landrop send a.zip --server URL --key KEY [--expire 小时] [--max-downloads N]`
  （或窗口里「经服务器」发送）会在服务端建一个临时传输（默认 24 小时内有效、取 1 次即失效），不会留下空间和授权；
  到期 / 取够次数 / 被撤销后，后台清理任务连同磁盘文件一起删除。管理面板的「一次性传输」里可以查看与撤销。
- **给没装任何东西的电脑**：服务起来后访问 `http://<地址>/downloads/landrop.pyz` 即可下载单文件客户端。
- **升级**：数据目录原样可用（数据库迁移只增不改）。旧版只有 `uploads/` 的话，用 `--import` 导入，**不要**直接删除旧目录。
- `LANDROP_PUBLIC_URL` 必须设为宿主机局域网地址，否则分享链接与文件码里是 Docker 内网地址、别人连不上。

---

## 权限模型

管理员持有**管理员密钥**，可以创建任意多条**分享链接**；每条链接独立授权：

| 权限档 | 能看到已有文件 | 下载 | 删除 | 说明 |
| --- | --- | --- | --- | --- |
| `仅上传` | ✗ | ✗ | ✗ | 投递箱：上传后只拿到自己的回执，看不到空间内容 |
| `仅下载` | ✓ | ✓ | ✗ | 只能取件，不能上传（一次性传输就是这一档） |
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

### `landrop serve` 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--data-dir` | 平台默认目录 | 数据目录（状态库、文件、分块、回收站）；默认与桌面窗口相同，也可用 `LANDROP_DATA_DIR`。旧版习惯的当前目录 `./data` 若已有状态库会继续沿用 |
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

`landrop gui --autostart`：打开窗口后立即启动收集服务。

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

除首页、`/admin` 与静态资源外，其余接口都需要登录（`HttpOnly` Cookie 会话）。**直连发送端说同一套最小接口**
（`login` / `files` / `download` / `done`），所以 `landrop get` 对两种来源通用。
`GET /<令牌>`（即用浏览器打开文件码）返回网页并自动登录；令牌随即从地址栏抹掉，响应带 `Referrer-Policy: no-referrer`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/login` | `{"key": "..."}` 密钥或口令；返回权限与空间 |
| `POST` | `/api/logout` | 退出登录 |
| `GET` | `/api/me` | 当前会话与能力（未登录返回 `logged_in:false`） |
| `GET` | `/api/files[?space=]` | 列出有权查看的文件（仅上传档返回空列表） |
| `PUT` | `/api/upload?name=&id=&offset=&total=&sha256=[&path=]` | 流式分块上传；`sha256` 可选但建议提供；`path` 为文件夹内相对路径 |
| `DELETE` | `/api/upload/abort?id=` | 放弃未完成的上传并清理分块 |
| `GET` | `/api/download?id=` | 下载，支持 `Range` 与 `X-File-SHA256` 响应头 |
| `POST` | `/api/done` | 接收端取完全部文件后调用；传输类授权据此累计次数（同一会话把文件都完整下载过也会自动计一次，不会重复） |
| `GET` | `/api/verify?id=` | 从磁盘重算摘要并与登记值比对 |
| `DELETE` | `/api/delete?id=` | 删除（按权限档判定，管理员可加 `&space=`） |
| `GET` / `POST` | `/api/grants` | 管理员：列出 / 创建授权 |
| `POST` | `/api/grants/revoke` | 管理员：撤销授权（`{"id": "..."}`，对传输同样有效） |
| `POST` | `/api/grants/rotate` | 管理员：轮换密钥并返回新明文 |
| `GET` / `POST` | `/api/spaces` | 管理员：列出 / 创建空间（不含传输的临时空间） |
| `GET` / `POST` | `/api/transfers` | 管理员：列出 / 创建一次性传输（`expires_hours`、`max_downloads`） |

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

- **直连发送端**：监听 `0.0.0.0` 的随机端口，令牌 96 位随机、常量时间比较，错误尝试 5 次即关闭；取件完成、超时或 Ctrl-C 后立即停止监听。
- **路径安全（接收端）**：对端给的路径逐段清洗，拒绝 `..` 与绝对路径，并处理 Windows 非法字符/保留名。
- **仍是明文 HTTP**：文件码等同凭证，抓包者可直接复制；请只在可信局域网内使用，或自行加 TLS 反代。

---

## 测试

```bash
python3 tests/smoke.py          # 服务端：权限矩阵、校验、续传、撤销、空间隔离、一次性传输与迁移
python3 tests/cli_check.py      # CLI：直连 send/get、经服务器 send、续传、一次性、撤销
python3 tests/gui_check.py      # 桌面窗口：驱动真实控件走完 发送/预览/接收/取消/经服务器/主题（Linux 用 xvfb-run）
python3 tests/package_check.py --cli <landrop> --gui <窗口程序>   # 对打包产物做直连/服务/窗口自检
```

打包：

```bash
pip install pyinstaller
pyinstaller --clean --noconfirm landrop.spec   # 一个应用目录：窗口 + 命令行
python packaging/package.py                    # 生成本平台的 安装版 + 免安装版（dist/release/）
python packaging/build_pyz.py                  # 单文件 landrop.pyz
```

CI 在 Linux、macOS、Windows 上跑前三套测试；每个平台构建安装版与免安装版后**真实安装、运行 `package_check.py`、再卸载**
（Windows 静默安装 + PATH 检查，macOS `installer -pkg`，Linux `apt install ./*.deb`），免安装版也会解压验证。
推 `vX.Y.Z` 标签会自动发布 Release 与 Docker 镜像。

---

## 目录结构

```
.
├── landrop/
│   ├── cli.py           # 统一入口：send / get / serve / revoke / gui
│   ├── common.py        # 共用：文件码、路径清洗、数据目录、进度、控制台
│   ├── direct.py        # 直连发送端（进程内临时 HTTP 监听 + 浏览器取件页）
│   ├── remote.py        # 客户端：上传 / 下载 / 创建传输（服务与直连通用）
│   ├── qr.py            # 二维码编码（窗口与 send --qr 共用）
│   ├── server/          # 收集服务：app.py（HTTP 与权限）、store.py（SQLite）、static/（网页）
│   └── gui/             # 桌面窗口（tkinter）：launcher.py（主窗口）、panels.py（页面）、
│                        #   widgets.py / theme.py（自绘控件与设计令牌）、system.py、windrop.py（Windows 拖放）
├── packaging/           # PyInstaller 入口、package.py（安装版 + 免安装版）、windows/ macos/ assets/、pyz 构建
├── tests/               # smoke / cli_check / gui_check / package_check
├── landrop.spec         # PyInstaller 配置：一个应用目录，窗口 + 命令行两个可执行文件
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## 常见问题

**Q：直连发送时对方连不上？**
A：先确认两台电脑在同一局域网；发送端系统防火墙首次会弹窗，请选「允许访问」；多网卡/VPN/Docker 环境下发送端会列出「其他可用地址」，把文件码里的 IP 换成可达的那个，或用 `--ip` 指定。

**Q：直连和「经服务器」有什么区别？**
A：直连不需要任何服务，但发送端必须在线直到对方取完；经服务器会把文件上传到常驻服务，对方随时可取，到期/取够次数后服务端自动清理。

**Q：对方是手机 / 没装 LAN Drop 怎么办？**
A：让对方扫发送页的二维码，或把「浏览器网址」发过去，用浏览器打开就能下载；多个文件可以一次打包成 zip。
反过来要让手机传给电脑，用「收集服务」，把访问地址（或二维码）给对方。

**Q：安装版里的命令行在哪？**
A：它和窗口是同一个程序。Windows 安装时默认把安装目录加入 PATH，新开终端输入 `landrop` 即可；macOS 安装包会链接
`/usr/local/bin/landrop`；Linux 是 `/usr/bin/landrop`。免安装版在窗口「设置 → 命令行工具」里一键加入 PATH。

**Q：免安装版的数据存在哪？**
A：Windows / Linux 免安装版存在程序目录的 `data/` 里（目录里有 `portable.txt` 时），整个文件夹拷走即可；
macOS 的 `.app` 内部不可写，所以仍用 `~/Library/Application Support/LANDrop`。

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
