# 运维、备份与恢复

[返回 README](../README.md)。以下命令在已配置好的 Linux 部署目录执行；数据权限与恢复命令需要 root。

## 一致性与失败恢复

SQLite 只由管理容器访问，开启 WAL、FULL 同步、外键和 5 秒 busy timeout；整个数据目录持有进程级独占锁，不能启动第二个写入实例。API 写操作另有进程内串行锁。

发布先写同目录临时文件，`fsync` 后 `os.replace`，随后同步目录。数据库与文件系统不具备共同事务，因此先提交操作意图，再修改发布文件，最后标记完成。重置 Token 先撤销旧文件，再发布新文件；已撤销的旧 Token 不会在恢复中重新启用。删除先撤下发布文件，再删除私有文件及元数据。

启动会重放未完成操作、移除孤立发布文件、校验当前私有文件 SHA-256、修复发布文件、清理超额历史及临时文件。恢复失败则拒绝启动；运行中存储失败后暂停写入，界面提示需恢复，健康检查失败。修正磁盘或权限问题后：

```sh
docker compose restart admin
docker compose ps
```

单独的公开容器仍会服务已存在的发布文件。**未返回成功的撤销操作不能视为已经完成**；管理端在撤销前崩溃时，旧文件可能在恢复前仍可读取。需要紧急阻止读取且无法恢复时，先 `docker compose stop public`，修复后先恢复 admin，再启动 public。

成功保证适用于操作完成后发起的新请求；已经开始的下载及用户已下载的副本无法撤回。Nginx 禁用打开文件缓存、ETag 和按修改时间返回的 304，避免同秒更新误用旧内容。

## 升级、回退和排障

升级前在管理页创建并下载一份加密备份，保留当前源代码、Compose 和镜像。新发布使用新的应用镜像标签，不把不同版本覆盖成同一个回退标签；当前生产标签为 `mihomo-config-manager-admin:0.3.0`。

更新 Python 依赖时修改输入文件，使用 pip-tools 重新生成两份带哈希锁文件，运行全部测试；更新基础镜像时同时修改精确版本和 digest，并在目标平台重新验收。不要只替换为 `latest`。

```sh
docker compose build admin
docker compose up -d --wait
docker compose ps
docker compose logs --tail=100 admin
docker stats --no-stream $(docker compose ps -q)
```

回退使用保存的旧版本代码、Compose 和旧镜像，保持相同 data 挂载；数据 schema 当前为 2，未来若发生不兼容迁移，必须按发布说明恢复升级前备份，不能强行让旧程序打开新 schema。程序会拒绝未知 schema。

| 现象 | 检查方向 |
|---|---|
| admin 启动失败 | 环境变量、data UID/权限、磁盘空间、第二个写入实例、数据库版本或文件校验 |
| 管理端 401 | Access 应用 AUD、Team Domain、允许策略、系统时间、JWT 是否传到源站 |
| 管理端 403 | Origin 与域名是否一致、刷新 CSRF、是否使用旧标签页 |
| 管理端 503 | JWKS 连通性，或磁盘/权限导致的待恢复操作；不要绕过认证 |
| 上传 413/422 | 大小、UTF-8、YAML mapping、语法或解析资源限制 |
| 上传 429 | 等当前上传完成后重试 |
| 订阅 404 | 链接是否完整/大小写正确、配置是否启用、Token 是否已重置、文件是否存在及可读 |
| 源站可读，客户端失败 | cfg 是否被 Access、WAF/Bot Challenge、缓存规则或 Worker 覆盖 |
| 更新后仍旧内容 | 客户端刷新、Cloudflare Bypass、响应不应为 HIT、是否用了旧链接 |

公开服务故意不记录包含 URL 的访问/错误日志。使用健康检查、`nginx -t` 和请求状态排障；不要为了排障直接打开请求日志。FastAPI 的请求遥测也明确禁用，避免外部 telemetry 配置收集内容或身份。

## 历史、回滚与加密备份

管理卡片中的“历史版本”展示当前及保留的历史，支持只读预览。回滚复制原始内容生成递增的新版本，保持当前名称、说明、Token、启停状态；采用所选版本的原始文件名。当前版本变化时返回 409，请刷新后重新选择。数据库自动从 schema 1 事务迁移至 schema 2；升级前保留旧镜像和数据备份，旧程序不能直接打开 schema 2。

管理页底部可手动创建加密备份、查询状态、下载和删除。密码为 16–256 个字符（空白也算密码内容），请另行妥善保管。密码遗失无法恢复；不提供密码找回、明文导出、自动删除或定时备份。归档包含 SQLite 快照、保留的全部私有版本及摘要清单；不包含 `.env`、公开目录、日志和其他备份。

备份位于 `data/backups`，仅 admin 挂载的数据目录可见，目录 700、文件 600。公开端没有备份挂载。快照阶段写请求返回 503，请稍后重试；随后释放存储锁进行压缩加密，公开下载不受影响。容量不足或中断的任务显示失败，需手动重试。新实例启动清理未完成暂存，不自动重试加密任务。

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `BACKUP_MAX_MIB` | `1024` | 单次备份内容上限 |
| `BACKUP_QUOTA_MIB` | `2048` | 已完成加密归档总容量上限 |

两项均为整数，范围 1–102400，单次上限不得大于总容量。创建前预留快照、压缩和加密空间；归档和暂存均需占用服务器磁盘。达到配额时手动删除不用的备份或调整设置，不会自动删除已完成备份。请将下载副本保存在服务器之外。

### 离线校验与恢复

先构建本次固定标签镜像：`docker compose build admin`。脚本使用 `mihomo-config-manager-admin:0.3.0`，只读根文件系统、无网络、不挂载 Docker socket；Docker 控制仅由宿主机上的脚本执行。命令需交互终端，密码隐藏输入，不能放在参数或环境变量中。

```sh
cd /opt/mihomo-config-manager
sudo sh scripts/backup.sh verify /opt/mihomo-config-manager/data/backups/备份ID.mcmbackup
sudo sh scripts/backup.sh restore /opt/mihomo-config-manager/data/backups/备份ID.mcmbackup
```

`verify` 不停止服务或改动生产数据。`restore` 先验证完整认证标签、归档结构、摘要、SQLite 及版本关系，再展示配置数量；输入 `RESTORE` 后停止本项目两容器。旧数据保留在 `data.before-restore`，新数据安装后先启动 admin，健康后才启动 public。全部 Token 换新，客户端必须更新链接；名称、原始内容、历史、启停状态保留。`.env` 不覆盖，迁移到新服务器需先单独配置域名和 Access 参数。

若备份大于默认上限，执行命令时可设置非秘密参数 `sudo BACKUP_MAX_MIB=2048 sh scripts/backup.sh verify /绝对路径/备份.mcmbackup`，恢复时使用相同上限。工具只支持 `.mcmbackup` 加密格式，不提供明文 tar 备份或恢复。

恢复中断后先检查磁盘、权限和容器状态，再运行：

```sh
sudo sh scripts/backup.sh resume
```

恢复阶段记录在 `.restore-state`，暂存目录为 `.restore-work`。工具不会自动重新发布旧数据；启动失败时公开容器保持停止。不要直接删除阶段记录或启动旧数据目录。下一次恢复前先妥善移走已有的 `data.before-restore`；它含旧 Token 和节点内容，不能公开或提交。未确认恢复前留下的暂存目录可在确认没有恢复命令运行后由管理员清理。

### 接口与备份格式

- `GET /api/configs/{id}/versions`、`GET /api/configs/{id}/versions/{version}/preview`。
- `POST /api/configs/{id}/versions/{version}/rollback`，JSON 为 `{"expected_current_version": 2}`。
- `POST /api/backups`，JSON 为 `{"password": "仅示意，不要使用此示例作为密码"}`，返回 202；不要在 shell 历史中提交真实密码。
- `GET /api/backups`、`GET /api/backups/{id}`、`GET /api/backups/{id}/download`、`DELETE /api/backups/{id}`。

所有管理 API 均要求 JWT，写操作要求 Origin 和 CSRF。备份 ID 不是下载凭据。错误不会回显密码、YAML 或 Token。

格式 v1：8 字节 `MCMBAK01` + 16 字节随机 salt + 12 字节随机 nonce + AES-256-GCM 密文 + 16 字节 tag。前 36 字节作为 AAD；scrypt 固定 N=65536、r=8、p=2，UTF-8 密码派生 32 字节密钥。密文为 gzip tar，内含 `manifest.json`、`db/manager.sqlite3` 和清单列出的私有版本。先认证整份文件，后解析归档；解密暂存仅在私有目录，解析拒绝路径穿越、链接、重复条目及超限展开。

### 恢复凭据校验

恢复工具严格拒绝含额外触发器、视图或改动表结构的备份。准备时会写入私有 `.restore-proof`，并在移动目录之前把本次恢复的指纹固定到数据目录之外的 `.restore-work/expected-proof`。续作启动前同时核对外部指纹、数据内凭据、数据库及发布文件；另一轮恢复留下的旧目录即使有自己的有效凭据，也不能通过。不要删除、复制替换或手工伪造恢复凭据。

续作校验失败时，请保持公开服务停止、保留旧数据和暂存目录，核实目录状态后从原始加密备份重新准备；不要通过把旧 data 移回并启动容器来绕过检查。若提示未 checkpoint 的 WAL，同样保持服务停止，先核查数据库与恢复记录，不要删除 WAL。

代码分发请使用 `scripts/package_release.py`，不要整目录打包：部署目录中的 `data.before-restore`、`.restore-work` 和环境文件都含 Token 或凭据。源码包不是数据备份，不能用于恢复订阅。
