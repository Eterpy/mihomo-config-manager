# 测试说明

所有测试使用临时目录、虚构 YAML 和运行时生成的签名密钥，不需要生产 `.env`、Cloudflare 账号、节点或备份。

## Python 测试

Linux 或 macOS，Python 3.14：

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m pytest -q -m 'not container' -p no:cacheprovider
.venv/bin/python -m pip check
```

覆盖认证、Origin/CSRF、上传限制、字节保真、版本保留与回滚、Token 撤销、磁盘/事务失败恢复、恶意备份、恢复续作和源码发布。Linux 专用 RLIMIT_AS 测试在 macOS 上跳过。

`backup.sh` 的控制流测试使用模拟 Docker 和真实 SQLite 核验，不等于已经运行真实容器恢复。Starlette 测试客户端可能输出 httpx 后端弃用警告，升级依赖时需要关注。

## 真实 Nginx HTTP

准备本机 Nginx 可执行文件后：

```sh
NGINX_BIN=/absolute/path/to/nginx .venv/bin/python -m tests.run_native
```

测试使用本机 18091–18093 端口；验证 GET/HEAD、响应头、原始字节、并发替换、启停与撤销、非法路径、历史回滚和备份隔离。退出自动清理临时进程与数据。它不能验证 Docker 的 UID、挂载或内存限制。

## 隔离容器验收

Linux Docker 主机上：

```sh
sh scripts/test-containers.sh
```

脚本构建固定镜像、运行 Python 测试、启动临时 admin/public、检查 Nginx 配置、运行 HTTP 用例并确认公开日志为空。没有宿主机端口或生产环境挂载，退出清理本次项目的容器和命名卷。仅初始化测试卷的容器使用 root，生产服务非 root。

测试 runner 的 `/tmp` 允许执行程序，因为 `backup.sh` 和预检脚本的测试会运行模拟的 `docker`、`id`、`flock` 等命令；这只用于一次性测试容器。初始化容器可能被 `docker compose run` 重复执行，它不会覆盖已生成的测试签名密钥。

GitHub Actions 的 `source-and-tests` 与 `containers` 任务分别对应上面的 Python 测试与隔离容器验收；仓库实际运行后的 CI 状态才代表该环境验证结果。

## 发布与语法检查

```sh
python3 scripts/check_public.py
# 在 git add 之后检查索引中实际将提交的内容：
python3 scripts/check_public.py --staged
python3 scripts/package_release.py /tmp/yaml-config-manager-source.tar.gz
node --check app/static/app.js
for script in scripts/*.sh; do sh -n "$script"; done
```

Node 仅用于语法检查，生产不需要。发布检查应与人工审查、Git 历史检查配合，不代表完整的秘密扫描。

## 手机布局与多标签验收

在装有 Python Playwright、Chromium 和 Jinja2 的测试环境中，从项目根目录运行：

```sh
python -m tests.browser_review
```

此回归使用真实的管理页模板、样式和脚本，以固定接口响应检查密码校验、备份及回滚错误的可见性、处理中配置的自动刷新和待恢复提示。备份持续轮询三个周期后会检查配置请求次数、键盘焦点和 SHA-256 展开状态，再模拟跨标签页修改，验证卡片重绘也能保留焦点和展开状态。复验还覆盖快照状态先于任务完成返回、状态或配置读取暂时失败、连续三次配置列表读取失败，以及手机视口中第 28 份备份的删除提示。

在本地临时预览中使用 Chromium 的手机设备模拟，分别设为 320、360、390、414 CSS 像素宽。检查配置卡片上的按钮都在卡片边框内，两行按钮的 `scrollWidth` 不超过 `clientWidth`，且整页 `document.documentElement.scrollWidth` 等于模拟视口宽度。打开历史和预览对话框检查内容滚动。

在两个同源标签页中打开同一配置的修改对话框，在第二页先保存说明，再提交第一页的旧表单。第一页应提示核对已刷新的状态，第二页写入的说明仍可见。打开配置列表时不请求订阅凭据，点击复制才调用链接接口。

## 未覆盖的范围

以下内容无法由仓库内测试验证，需要部署者在目标环境验收：

- 真实 Docker 容器中的交互式恢复与中断续作（`backup.sh` 的控制流测试使用模拟 Docker）。
- Cloudflare Access 身份策略、Tunnel、Cache Bypass 与真实客户端的更新和撤销。
- 宿主机端口从外部不可达、目标平台的资源限制与实际镜像系统包。

固定依赖与镜像不代表持续不存在已知漏洞；发布前应另外检查当前漏洞数据库和实际镜像系统包。

## 最近验证记录

| 日期 | 环境 | 检查 | 结果 |
|---|---|---|---|
| 2026-11-28 | macOS，Python 3.14，Bash 3.2 / dash | 0.4.1 复验修复：轮询时序、列表 503 重试、弹窗与长列表提示、历史清理及续作、不可解析挂载、可压缩备份的重新发布空间 | 非容器测试各 287 passed、1 skipped（Linux 内存限制），本机 Nginx HTTP 各 6 passed，Chromium 回归通过。Docker Engine 和真实读屏软件未验收 |
| 2026-11-28 | macOS，Python 3.14，Bash 3.2 / dash | 0.4.1 目录校验、API 错误、归档并发、挂载冲突、启动诊断及管理页交互 | 两种 shell 下非容器测试各 283 passed、1 skipped（Linux 内存限制），本机 Nginx HTTP 各 6 passed，Chromium 回归均通过。恢复控制器使用 Docker 桩，本机缺少 Docker 引擎，未执行隔离容器验收 |
| 2026-11-28 | macOS，Python 3.14，Bash 3.2 / dash | 0.4.0 提交前：恢复前的应用配置校验只在停服前运行（`restore`，以及 `data` 仍在原处的 `prepared` 续作），不再依赖匿名卷覆盖 `/data` | 非容器 263 passed、1 skipped（Linux 内存限制）；恢复控制器测试在两种 shell 下各 42 passed。Docker 生命周期使用桩命令，Linux 实机恢复演练待验证 |
| 2026-11-28 | macOS，Python 3.14，Bash 3.2 / dash | 0.4.0 最终审查：真实 SQLite 恢复核验、public 启动失败后保留停用操作、信号清理、已停止项目冲突、配置预检、快照并发读取 | 非容器 258 passed、1 skipped（Linux 内存限制）；两种 shell 的相关测试各 101 passed。暂停快照复制时，本机列表和取链接各约 1.5 ms |
| 2026-11-28 | Chromium、本机 Nginx | 重新登录、状态提示消退、改名不可写及冲突后保留输入、重复回滚、HTTP 下载 | 浏览器场景通过，真实 HTTP 6 passed。Docker 生命周期使用桩命令；Linux 实机断线清理与恢复演练待验证 |
| 2026-11-28 | macOS，Python 3.14，Chromium、本机 Nginx | 文件校验与修复提示、损坏条目隔离、删除后的旧库提示、并发分段校验、刷新期间追加拖入 | 非容器 248 passed、1 skipped；真实 HTTP 6 passed。8 个并发 Range 与 1 个 HEAD 共计算一次完整摘要；替换归档后重新校验，同大小篡改被拒绝。Chromium 中两种修复提示及追加拖入通过 |
| 2026-11-28 | macOS，Python 3.14，0.4.0 最新复查 | 存储启动一致性、同内容修复、离线目标目录竞争、备份 Range / HEAD、镜像 COPY 清单、预检 | 非容器 246 passed、1 skipped（Linux 内存限制）。本机 Nginx HTTP 6 passed。0.3.1 归档经新版准备后可启动 |
| 2026-11-28 | 本机 Chrome，临时预览 | 快照结束自动刷新、关闭回滚窗口、删除配置、混合批量上传及追加拖入、确认期间改写磁盘文件 | 通过。关闭的窗口保持关闭，已删除配置的历史清空，无未捕获异常；上传保存了确认时的文件字节 |
| 2026-11-28 | Docker 构建验收 | CI 构建两个镜像并按发布清单核对 `/opt/app` 内的实际文件 | 本机缺少 Docker，实际构建及 GitHub Actions 仍待验证 |
| 2026-11-28 | macOS，Python 3.14，Compose v5.7.1 | 非容器测试、停服后免传解密上限续作、对照 Compose 解析带引号、注释、export 和 CRLF 的数值 | 227 passed、1 skipped（Linux 内存限制）。UTF-8 下恢复控制器在 Bash 3.2 和 dash 各 26 passed。停服及续作使用桩命令 |
| 2026-11-28 | macOS，0.4.0 推送前复查 | 非容器测试、真实 Nginx HTTP、UTF-8 下的恢复脚本、归档哈希期间并发列出和删除 | 211 passed、1 skipped（Linux 内存限制）；HTTP 6 passed；恢复控制器用例在 Bash 3.2 和 dash 下分别 16 passed |
| 2026-11-28 | Chromium、Playwright WebKit 26 | 点击时发起剪贴板写入、延迟取得链接、手动复制回退、断网重连、登录跳转 | 两种引擎均通过。剪贴板写入使用测试替身，WebKit 登录跳转使用本地 HTTP 302 |
| 2026-11-28 | macOS，Python 3.14，0.4.0 | 非容器测试、真实本机 Nginx HTTP、依赖与语法、Compose 配置 | 205 passed、1 skipped（Linux 内存限制）；HTTP 6 passed |
| 2026-11-28 | Chromium 手机模拟与同源标签页 | 四种手机宽度、按需复制、并发改名、CSRF 续期和标签页刷新 | 通过 |
| 2026-11-28 | 0.3.1 → 0.4.0 | 从旧提交生成加密归档，运行新版离线准备和存储启动 | 配置、两个版本、停用状态均保留，Token 已轮换 |
| 2026-11-28 | macOS，0.4.0 | Linux Docker 隔离验收 | 本机没有 Docker，未执行；恢复脚本停服前检查、中断清理与续作已使用桩命令验证 |
| 2026-11-18 | macOS，Python 3.14，0.3.1 版本号更新后 | 全量 Python 测试；恢复相关测试另以 dash 运行；Shell / JS 语法；发布检查与源码打包 | 通过；158 passed、1 skipped（RLIMIT_AS）；dash 下 61 passed；70 个文件无规则命中 |
| 2026-11-18 | GitHub Actions，`main` 提交 `0a9d283`（与 0.3.1 仅差版本号） | `source-and-tests` / `containers` | 通过；159 passed；容器内 155 passed，HTTP 6 passed |
| 2026-11-18 | GitHub Actions，`main` 提交 `f71f175` | `source-and-tests`：发布检查、全量 Python 测试、语法与依赖检查、源码打包 | 通过；156 passed（Linux 上含 RLIMIT_AS 测试） |
| 2026-11-18 | GitHub Actions，`main` 提交 `f71f175` | `containers`：容器内 Python 测试、真实 HTTP 用例、Nginx 配置、公开日志为空 | 通过；容器内 152 passed，HTTP 6 passed |
| 2026-11-17 | macOS，本机 Nginx 1.30.6 | `tests.run_native` 真实 HTTP | 配置检查通过；6 passed |

测试输出保留一条 Starlette 测试客户端的 httpx 弃用警告。
