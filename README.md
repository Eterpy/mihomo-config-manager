# YAML Config Manager

自托管的 YAML 配置管理与只读订阅服务，应用代码采用 [MIT 许可证](LICENSE)。适合个人或受信任的小团队集中维护配置，并通过独立链接供客户端订阅。

A self-hosted YAML configuration manager with version history, encrypted backups, and token-protected subscriptions. The interface and documentation are in Chinese.

## 功能

- 上传、拖拽批量上传、搜索、纯文本预览、修改名称和说明。
- 按原始字节保存 YAML，保留注释、BOM 和换行格式；更新内容保持订阅 URL 不变。
- 每份配置使用独立的 256 位随机 Token，支持启停、撤销旧 Token、删除。
- 当前版本与可配置数量的历史版本；回滚生成新版本，保持当前 Token 和启停状态。
- 手动加密备份、下载与离线恢复；恢复自动换新全部订阅 Token。
- 中文响应式界面，浅色、深色和跟随系统主题，无外部前端资源。

当前认证方式为 **Cloudflare Access**，生产部署需要自己的 Cloudflare 账号、域名、Access 应用和 Tunnel。没有本地密码登录或生产免认证模式。所有通过 Access 策略的身份共享同一配置库和管理权限；本项目不提供多租户隔离。

本工具管理配置文件，代理内核由客户端提供。上传时校验 YAML 语法和基本格式，配置的运行效果由客户端验证。

**订阅链接就是读取凭据。**任何持有完整链接的人都能下载其中的节点信息。请勿把真实 YAML、订阅 URL、环境配置或备份提交到公开仓库。

## 架构

```text
浏览器 → admin.example.com → Cloudflare Access → Tunnel
        → 127.0.0.1:18081 → admin:8000
                            ├─ SQLite
                            ├─ 私有配置与历史
                            ├─ 加密备份
                            └─ 原子发布文件

客户端 → subscribe.example.com → Tunnel（绕过缓存，无交互式挑战）
        → 127.0.0.1:18080 → public:8080 → 只读 published
```

所有 `example.com` 域名均为占位符，请替换为自己的域名。管理端为 Python 3.14 / FastAPI / SQLite / 原生 JS；订阅端为非 root Nginx。生产 Compose 仅包含这两个服务，cloudflared 在宿主机独立运行。两容器使用只读根文件系统并移除 capabilities，宿主机端口仅绑定 loopback。

## 快速体验

Linux 或 macOS，Python 3.14，在项目根目录执行：

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m tests.preview
```

打开 `http://127.0.0.1:18089`。预览仅使用临时样例和临时签名密钥，退出后清理。不要为这个端口添加公网代理或 Tunnel 路由。预览入口不包含在生产镜像中。原生代码使用 POSIX 文件锁，Windows 请通过 Linux 环境运行。

## 部署

需要 Linux 主机、Docker Engine 28 或更新版本、Docker Compose v2+、可用的 `ss` / `flock` / POSIX shell，以及宿主机上的 cloudflared。无需任何特定服务器面板、SSH 账号或固定 SSH 端口。

1. 克隆本仓库到自己的服务器。
2. 为管理域名创建 Cloudflare Access 应用，配置允许登录的身份。
3. 复制 `.env.example` 为 `.env`，填写 Access 与域名参数。
4. 运行隔离测试、初始化数据目录、预检并启动：

   ```sh
   sh scripts/test-containers.sh
   sudo sh scripts/init-data.sh
   sudo sh scripts/preflight.sh
   sudo docker compose up -d --build --wait --wait-timeout 120
   ```

5. 将两个域名分别接入 Tunnel，为订阅域名配置 Cache Bypass，完成上线验收。

完整步骤、环境变量表与验收清单见 [部署指南](docs/deployment.md)。

## 使用

- **上传**：选择或拖入 `.yaml` / `.yml` 文件即可创建配置，并将订阅链接导入支持该配置格式的客户端。若完整文件名（含扩展名、区分大小写）与已有配置的当前文件名相同，页面会让你选择更新、另存为独立订阅或取消。多份配置同名时，先选择要更新的配置；批量上传时取消只跳过当前文件，期间再次拖入的文件会依次加入队列。
- **更新**：在配置卡片上「上传新版本」，订阅链接不变，客户端刷新即可得到新内容。文件内容（按 SHA-256 判断）与当前版本相同且当前文件完整时只提示；当前文件缺失或损坏时会重新生成版本并修复文件；与保留的历史版本相同时会提示是哪个版本，由你决定是否继续上传。
- **历史与回滚**：「历史版本」可预览当前及保留的旧版本。回滚会复制所选版本生成新版本，保持当前名称、说明、Token 和启停状态。
- **停用、重置与删除**：停用后链接返回 404，启用后恢复；「重置 Token」使旧链接永久失效，需在客户端换成新链接；删除会同时移除全部历史版本。

复制订阅链接时，管理页才向服务器领取这份配置的凭据。多标签页可以同时管理配置；若另一页先保存了修改，请核对最新状态后重试。

公开端只接受精确的 `/s/<Token>/profile.yaml` 路径和 GET / HEAD 请求，不接受查询参数。管理 API 与管理页使用同一套认证，见 [接口说明](docs/api.md)。

## 备份与恢复

在管理页创建加密备份并下载到服务器之外，密码另行保管，遗失无法恢复。恢复在宿主机上离线执行，并会**换新全部订阅 Token**：

```sh
sudo sh scripts/backup.sh verify /absolute/path/backup.mcmbackup
sudo sh scripts/backup.sh restore /absolute/path/backup.mcmbackup
sudo sh scripts/backup.sh resume    # 恢复中断后续作
```

备份策略、迁移到新机器、恢复后检查清单与故障处理见 [备份与恢复](docs/backup-restore.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [部署指南](docs/deployment.md) | 主机要求、Access、环境变量、启动、Tunnel 与上线验收 |
| [运维指南](docs/operations.md) | 日常检查、一致性与故障恢复、升级回退、排障 |
| [备份与恢复](docs/backup-restore.md) | 备份策略、同机恢复、迁移、演练与故障对照 |
| [接口说明](docs/api.md) | 认证要求、管理 API、错误码与公开订阅端 |
| [发布指南](docs/releasing.md) | 源码打包、发布检查与版本发布 |
| [测试说明](TESTING.md) | 测试层级、命令与最近验证记录 |
| [贡献指南](CONTRIBUTING.md) | 开发环境、提交要求、依赖更新与文档分工 |
| [安全政策](SECURITY.md) / [安全复审记录](SECURITY_REVIEW.md) | 漏洞报告、安全边界、已修复问题 |
| [变更记录](CHANGELOG.md) | 各版本变化 |

## 开发

```sh
.venv/bin/python -m pytest -q -m 'not container'
python3 scripts/check_public.py
python3 scripts/package_release.py /tmp/yaml-config-manager-source.tar.gz
```

源码包按白名单生成，不含 `.git`、虚拟环境、数据、备份和恢复目录。提交要求见 [贡献指南](CONTRIBUTING.md)。

本项目独立维护，与 MetaCubeX、Clash 或 Cloudflare 无隶属关系。导航图标使用 MetaCubeX 上游的图标，等比缩小至 126 像素，随附上游许可。[图标来源](https://github.com/MetaCubeX/Clash.Meta/blob/0455d0d28c4526937380505153e65a843d8612e8/Meta.png) · [GPL-3.0 许可](app/static/brand-license.txt)
