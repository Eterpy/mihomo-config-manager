# Mihomo Config Manager

自托管的 Mihomo YAML 配置管理与只读订阅服务，采用 [MIT 许可证](LICENSE)。适合个人或受信任的小团队集中维护配置，并通过独立链接供客户端订阅。

A self-hosted Mihomo YAML configuration manager with version history, encrypted backups, and token-protected subscriptions. MIT licensed. The current interface and documentation are in Chinese.

## 功能

- 上传、拖拽批量上传、搜索、纯文本预览、修改名称和说明。
- 按原始字节保存 YAML，保留注释、BOM 和换行格式；更新内容保持订阅 URL 不变。
- 每份配置使用独立的 256 位随机 Token，支持启停、撤销旧 Token、删除。
- 当前版本与可配置数量的历史版本；回滚生成新版本，保持当前 Token 和启停状态。
- 手动加密备份、下载与离线恢复；恢复自动换新全部订阅 Token。
- 中文响应式界面，浅色、深色和跟随系统主题，无外部前端资源。

当前认证方式为 **Cloudflare Access**，生产部署需要自己的 Cloudflare 账号、域名、Access 应用和 Tunnel。没有本地密码登录或生产免认证模式。所有通过 Access 策略的身份共享同一配置库和管理权限；本项目不提供多租户隔离。

本工具不包含 Mihomo 内核，不提供代理节点、订阅转换或在线编辑器。YAML 校验限于安全解析与基本格式，不代表配置已通过 Mihomo 语义校验。

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

需要 Linux 主机、Docker Engine 28 或更新版本、Docker Compose v2+、可用的 `ss` / `flock` / POSIX shell，以及宿主机上的 cloudflared。无需任何特定服务器面板、SSH 账号或固定 SSH 端口。详细步骤见 [部署指南](docs/deployment.md)。

1. 从本仓库下载源码或克隆到自己的服务器。
2. 为管理域名创建 Cloudflare Access 应用，配置允许登录的身份。
3. 复制示例并填写部署参数：

   ```sh
   cp .env.example .env
   chmod 600 .env
   # 用编辑器填写 .env 中的四项部署参数。
   ```

4. 运行隔离测试，初始化权限、预检并启动：

   ```sh
   sh scripts/test-containers.sh
   sudo sh scripts/init-data.sh
   sudo sh scripts/preflight.sh
   sudo docker compose up -d --build --wait --wait-timeout 120
   ```

5. 将两个域名分别接入 Tunnel，订阅域名配置 Cache Bypass，再按部署指南验证身份、更新和撤销。

示例值或缺失的部署域名会导致生产入口拒绝启动。`APP_ENV=development` 也需要真实认证参数。直接用 Uvicorn 启动时，须由调用环境提供变量；应用不会自动加载 `.env`，Compose 会加载。

| 变量 | 要求 / 默认值 |
|---|---|
| `CF_ACCESS_TEAM_DOMAIN` | 必填，例如自己的 `TEAM.cloudflareaccess.com` |
| `CF_ACCESS_AUD` | 必填，管理端 Access 应用的 AUD |
| `ADMIN_HOSTNAME` | 必填，管理域名，无协议、端口或路径 |
| `PUBLIC_BASE_URL` | 必填，订阅域名的 HTTPS URL；与管理域名不同，无端口、路径、凭据或查询参数 |
| `APP_ENV` | `production`；也接受 `development`，不影响认证要求 |
| `MAX_UPLOAD_MIB` | `10`；范围 1–100 |
| `HISTORY_VERSIONS` | `5`；当前版本以外的保留数量，范围 0–100 |
| `YAML_TIMEOUT_SECONDS` | `5`；大于 0 且不超过 30 |
| `YAML_MEMORY_MIB` | `256`；范围 64–1024，Linux 解析子进程使用 RLIMIT_AS |
| `BACKUP_MAX_MIB` | `1024`；单次备份内容上限 |
| `BACKUP_QUOTA_MIB` | `2048`；完成的备份总容量上限，不小于单次上限；两者最大 102400 |
| `DATA_DIR` | Compose 固定为 `/data`；直接运行时默认工作目录下的 `data` |

提高上传或备份限制后，应重新检查容器内存、临时空间和磁盘容量。程序只支持一个写入实例、一个 Uvicorn worker，不可通过增加副本共享同一个数据目录。

## 使用与 API

登录管理页后上传 `.yaml` / `.yml` 文件，复制订阅链接，在支持 Mihomo YAML 的客户端中导入。上传新版本后刷新客户端即可；重置 Token 后需要更新客户端链接。公开端仅支持精确的 `/s/<Token>/profile.yaml` 路径和 GET/HEAD，不接受查询参数。

所有管理 API、页面、静态资源与 `/openapi.json` 都要求有效 Access JWT。写请求还要求正确的 Origin、CSRF Header 与 Cookie；CSRF 由首页生成，过期或进程重启后刷新页面。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/status` | 就绪状态和限额 |
| GET / POST | `/api/configs` | 列表 / multipart 上传（`file`，可选 `name`、`description`） |
| GET | `/api/configs/{id}`、`/api/configs/{id}/preview` | 元数据 / 纯文本内容 |
| PUT | `/api/configs/{id}/content` | 上传新版本，保持 URL |
| PATCH | `/api/configs/{id}` | 修改名称、说明 |
| POST | `/api/configs/{id}/enable`、`disable`、`rotate-token` | 启停 / 重置 Token |
| DELETE | `/api/configs/{id}` | 删除配置与历史 |
| GET | `/api/configs/{id}/versions`、`/api/configs/{id}/versions/{version}/preview` | 历史与预览 |
| POST | `/api/configs/{id}/versions/{version}/rollback` | 回滚；JSON 含 `expected_current_version` |
| GET / POST | `/api/backups` | 列表 / 创建；创建 JSON 含 `password` |
| GET / DELETE | `/api/backups/{id}` | 状态 / 删除 |
| GET | `/api/backups/{id}/download` | 下载加密备份，仍需 JWT |

API 错误返回 `{"detail":"说明"}`：401 认证失败、403 来源或 CSRF 失败、409 冲突、413 超限、422 校验失败、429 并发受限、503 服务或存储暂不可用。错误不会回显 YAML 内容。

## 备份与运维

在管理页创建加密备份，使用 16–256 字符密码，下载副本并单独保管密码。密码遗失无法恢复。备份包含数据库快照和保留的私有版本，不含 `.env` 或公开目录。

```sh
sudo sh scripts/backup.sh verify /absolute/path/backup.mcmbackup
sudo sh scripts/backup.sh restore /absolute/path/backup.mcmbackup
# 恢复中断时，检查状态后续作：
sudo sh scripts/backup.sh resume
```

恢复会停止本项目服务、保留旧目录并换新全部 Token。操作需要交互终端，密码隐藏输入。完整的恢复、升级和故障边界见 [运维指南](docs/operations.md)。

## 开发与发布

```sh
.venv/bin/python -m pytest -q -m 'not container'
python3 scripts/check_public.py
python3 scripts/package_release.py /tmp/mihomo-config-manager-source.tar.gz
```

源码包按白名单生成，不含 `.git`、虚拟环境、数据、备份和恢复目录。新增文档或发布文件时同步更新白名单。Git 忽略规则不能移除已被提交的内容；发布工具也不能识别所有形式的硬编码秘密。

- [贡献指南](CONTRIBUTING.md)：开发、提交与依赖更新。
- [测试说明](TESTING.md)：可复现命令与验证范围。
- [发布指南](docs/releasing.md)：首次公开仓库与后续发布。
- [安全政策](SECURITY.md) / [安全复审记录](SECURITY_REVIEW.md)。
- [变更记录](CHANGELOG.md) / [MIT 许可证](LICENSE)。

本项目独立维护，与 Mihomo、Clash 或 Cloudflare 无隶属关系。
