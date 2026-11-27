# 运维指南

[返回 README](../README.md)。以下命令在已配置好的 Linux 部署目录执行；修改数据权限需要 root。备份、恢复与迁移见 [备份与恢复](backup-restore.md)。

## 日常检查

```sh
docker compose ps                                  # admin 与 public 应为 healthy
docker compose logs --tail=100 admin
docker stats --no-stream $(docker compose ps -q)
```

管理页顶部的「运行状态」显示「运行正常」时可以正常写入；显示「需要恢复」说明存储存在未完成操作，按下一节处理。

## 一致性与失败恢复

SQLite 只由管理容器访问，开启 WAL、FULL 同步、外键和 5 秒 busy timeout；整个数据目录持有进程级独占锁，不能启动第二个写入实例。API 写操作另有进程内串行锁。

发布先写同目录临时文件，`fsync` 后 `os.replace`，随后同步目录。数据库与文件系统不具备共同事务，因此先提交操作意图，再修改发布文件，最后标记完成。重置 Token 先撤销旧文件，再发布新文件；已撤销的旧 Token 不会在重启恢复中重新启用。删除先撤下发布文件，再删除私有文件及元数据。

启动会重放未完成操作、移除孤立发布文件、校验当前私有文件 SHA-256、修复发布文件、清理超额历史及临时文件。恢复失败则拒绝启动；运行中存储失败后暂停写入，界面提示需恢复，健康检查失败。修正磁盘或权限问题后：

```sh
docker compose restart admin
docker compose ps
```

单独的公开容器仍会服务已存在的发布文件。**未返回成功的撤销操作不能视为已经完成**；管理端在撤销前崩溃时，旧文件可能在恢复前仍可读取。需要紧急阻止读取且无法恢复时，先 `docker compose stop public`，修复后先恢复 admin，再启动 public。

成功保证适用于操作完成后发起的新请求；已经开始的下载及用户已下载的副本无法撤回。Nginx 禁用打开文件缓存、ETag 和按修改时间返回的 304，避免同秒更新误用旧内容。

## 升级与回退

升级前：

1. 在管理页创建并下载一份加密备份，见 [备份与恢复](backup-restore.md)。
2. 保留当前源代码、Compose 文件和镜像。每个版本使用自己的应用镜像标签（见 `compose.yaml` 中 admin 的 `image`），不要把不同版本覆盖成同一个标签，以便回退。
3. 已有部署首次升级到默认项目名为 `yaml-config-manager` 的版本时，运行 `docker compose ls --all`，按部署目录核对现有项目的 `NAME`。在原部署目录的 `.env` 中将 `COMPOSE_PROJECT_NAME` 设为这个值，继续使用原目录和 `data` 挂载。这样下次 `up` 会更新原有容器，恢复脚本也会操作同一项目。

升级：

```sh
git fetch --tags
git checkout v新版本
docker compose build admin
docker compose up -d --wait
docker compose ps
```

数据库 schema 当前为 2；从 schema 1 启动时会在事务中自动迁移。旧程序不能打开新 schema，程序也会拒绝未知 schema。因此回退时使用保存的旧版本代码、Compose 和旧镜像并保持相同 data 挂载；若新版本发生过不兼容的 schema 迁移，必须按发布说明恢复升级前的备份，不能强行让旧程序打开新数据。

依赖与基础镜像的更新方法见 [贡献指南](../CONTRIBUTING.md)。

## 排障

| 现象 | 检查方向 |
|---|---|
| admin 启动失败 | 环境变量、data UID/权限、磁盘空间、第二个写入实例、数据库版本或文件校验 |
| 管理端 401 | Access 应用 AUD、Team Domain、允许策略、系统时间、JWT 是否传到源站 |
| 管理端 403 | Origin 与域名是否一致、刷新 CSRF、是否使用旧标签页 |
| 管理端 503 | JWKS 连通性、磁盘/权限导致的待恢复操作，或正在创建备份快照；不要绕过认证 |
| 上传 413/422 | 大小、UTF-8、YAML mapping、语法或解析资源限制 |
| 上传 429 | 等当前上传完成后重试 |
| 订阅 404 | 链接是否完整/大小写正确、配置是否启用、Token 是否已重置、文件是否存在及可读 |
| 源站可读，客户端失败 | 订阅域名是否被 Access、WAF/Bot Challenge、缓存规则或 Worker 覆盖 |
| 更新后仍旧内容 | 客户端刷新、Cloudflare Bypass、响应不应为 HIT、是否用了旧链接 |
| 备份失败或恢复报错 | 见 [备份与恢复](backup-restore.md) 的「中断、续作与故障对照」 |

## 日志与遥测

公开服务故意不记录包含 URL 的访问/错误日志；管理端关闭访问日志，FastAPI 的请求遥测也明确禁用，避免外部 telemetry 配置收集内容或身份。使用健康检查、`nginx -t` 和请求状态码排障，不要为了排障打开请求日志，也不要把真实订阅链接写进 shell 历史或公开日志。
