# 接口说明

[返回 README](../README.md)。管理 API 与管理页共用同一套认证；公开订阅端只提供只读下载。

## 认证与通用要求

所有管理页面、静态资源、`/openapi.json` 和 `/api/*` 都要求：

- `Host` 与 `ADMIN_HOSTNAME` 完全一致，否则返回 400。
- 请求头 `Cf-Access-Jwt-Assertion` 携带有效的 Cloudflare Access JWT。服务端独立校验 RS256 签名、issuer、audience、有效期和非空的 `sub`，不信任邮箱等身份头。公钥只从 `CF_ACCESS_TEAM_DOMAIN` 获取。

写请求（POST、PUT、PATCH、DELETE）另外要求：

- `Origin` 等于 `https://ADMIN_HOSTNAME`。
- 请求头 `X-CSRF-Token` 携带与当前身份绑定的签名凭据，有效期 12 小时。每个标签页可独立使用。管理端重启或凭据过期后，收到 403 且 `code=csrf` 时调用 `GET /api/csrf` 更新凭据，再重试一次。Origin 校验失败需修正请求来源。

本项目只在浏览器登录场景下验证过。用 Access 服务令牌等非交互方式调用时，需自行确认签发的 JWT 带有非空的 `sub`，否则会被拒绝。

其他限制：

- 上传请求体上限为 `MAX_UPLOAD_MIB` 加 64 KiB，其他请求为 64 KiB。
- 同一时间只处理一个上传，并发上传返回 429，并带 `Retry-After: 2`。
- 备份快照阶段的写请求返回 503。列表、详情、订阅链接、当前及历史预览可并发读取；如读取的文件恰被清理，预览返回 503。
- 所有响应带 `Cache-Control: private, no-store` 和严格的 CSP；不提供 `/docs`、`/redoc` 交互文档。

## 管理端点

| 方法 | 路径 | 请求 | 成功响应 |
|---|---|---|---|
| GET | `/api/csrf` | — | `{"csrf_token": "..."}` |
| GET | `/api/status` | — | `ready`、`max_upload_bytes`、`history_versions`、`snapshotting` |
| GET | `/api/configs` | — | 配置列表 |
| POST | `/api/configs` | multipart：`file`（必填，`.yaml`/`.yml`），可选 `name`（≤100 字符，默认取去除空白的文件主名前 100 字符）、`description`（≤2000 字符） | 201，配置对象 |
| GET | `/api/configs/{id}` | — | 配置对象 |
| POST | `/api/configs/{id}/subscription-link` | Origin 和 CSRF | `{"subscription_url": "..."}`；快照期间可读 |
| GET | `/api/configs/{id}/preview` | — | 当前版本原始内容，`text/plain` |
| PUT | `/api/configs/{id}/content` | multipart 只接受 `file`；必填查询参数 `expected_current_version` 为正整数 | 新版本的配置对象，沿用原订阅 URL；传入版本与当前版本不符时返回 409；内容相同且当前私有文件完整时，先检查并修复已启用的公开副本，再返回 409、`code=unchanged`，不生成新版本 |
| PATCH | `/api/configs/{id}` | JSON：必填 `expected_updated_at`，仅提交有改动的 `name`、`description` | 配置对象 |
| POST | `/api/configs/{id}/enable` | 查询参数 `expected_updated_at` | 配置对象 |
| POST | `/api/configs/{id}/disable` | 查询参数 `expected_updated_at` | 配置对象 |
| POST | `/api/configs/{id}/rotate-token` | 查询参数 `expected_updated_at` | 配置对象；通过链接接口领取新 URL |
| DELETE | `/api/configs/{id}` | 查询参数 `expected_updated_at` | 204，同时删除历史版本 |
| GET | `/api/configs/{id}/versions` | — | 版本列表，新版本在前 |
| GET | `/api/configs/{id}/versions/{version}/preview` | — | 该版本原始内容 |
| POST | `/api/configs/{id}/versions/{version}/rollback` | JSON：`{"expected_current_version": 2}` | 配置对象；当前版本已变化或目标就是当前版本时返回 409；目标版本内容与当前版本相同且当前私有文件完整时返回 409、`code=unchanged` |
| GET | `/api/backups` | — | `items`、`used_bytes`、`quota_bytes`、`max_bytes` |
| POST | `/api/backups` | JSON：`{"password": "16–256 个字符"}` | 202，备份任务对象 |
| GET | `/api/backups/{id}` | — | 备份任务对象 |
| GET / HEAD | `/api/backups/{id}/download` | 可选 `Range`、`If-Range` | 完整文件 200，单段范围 206，无法满足范围 416；HEAD 仅返回响应头。归档未完成、缺失或 SHA-256 不符时返回 409 |
| DELETE | `/api/backups/{id}` | — | 204；任务进行中时返回 409 |

配置和备份任务的 `{id}` 为 UUID。未登记归档使用列表给出的 `file-…` ID，仅供手动删除。`{version}` 为正整数。不要把真实备份密码写进 shell 历史或脚本。备份 ID 不是下载凭据，下载同样需要登录。

备份下载支持 `bytes=start-end`、`bytes=start-` 和 `bytes=-suffix`。首次下载先校验完整归档。同一管理进程内，GET、HEAD 和并发分段请求共享成功的校验结果；文件被替换、大小或时间戳改变，或管理端重启后会重新计算 SHA-256。成功响应带 `Accept-Ranges: bytes` 和由 SHA-256 构成的强 ETag。206 返回所选区间的 `Content-Range` 与长度；416 返回 `Content-Range: bytes */总长度`。多段请求或 `If-Range` 不匹配时返回完整文件。

上传或回滚的内容与数据库摘要相同，但当前私有文件缺失或损坏时，会从经校验的相同字节修复保留的版本并生成新版本。仍在磁盘上的损坏条目先移入隔离区供检查。

### 返回对象

**配置对象**：`id`、`name`、`description`、`filename`、`size`、`sha256`、`version`、`enabled`、`status`（`ready` / `pending` / `deleting`）、`created_at`、`updated_at`。订阅凭据只在按需读取链接时返回。

**版本对象**：`config_id`、`version`、`filename`、`size`、`sha256`、`created_at`、`source_version`（由回滚生成时为来源版本，否则为空）、`is_current`。

**备份任务对象**：`id`、`status`（`queued` / `snapshotting` / `encrypting` / `ready` / `failed` / `unregistered`）、`created_at`、`size`、`sha256`、`error_code`。

## 错误

API 错误返回 `detail`。文件名按解码后的字符数计，最长 255 字符；相关错误码为 `filename_invalid`、`filename_too_long` 或 `name_invalid`。对已有配置执行修改时，使用上次读取的版本号或原样传回 `updated_at`（查询参数需 URL 编码）；过期返回 409、`code=stale` 和 `current` 配置对象。重新读取、核对后再提交。

| 状态码 | 含义 |
|---|---|
| 400 | 请求格式错误或管理域名不匹配 |
| 401 | 缺少或无效的 Access JWT |
| 403 | Origin 或 CSRF 校验失败 |
| 404 | 资源不存在 |
| 409 | 名称冲突、版本已变化（`code=stale`）、内容与当前版本相同（`code=unchanged`）、任务状态不允许 |
| 413 | 超过大小限制 |
| 422 | 参数或 YAML 校验失败 |
| 428 | 缺少版本或修改时间前置条件 |
| 429 | 已有上传或备份任务在执行 |
| 503 | 认证公钥暂不可用、存储需要恢复或正在创建快照 |

## OpenAPI 与健康检查

- `GET /openapi.json`：需要 JWT，标注了各端点的认证与 CSRF 要求。
- `GET /healthz`：只接受来自容器内回环地址的请求，供 Compose 健康检查使用，外部访问返回 404。

## 公开订阅端

订阅域名只提供：

```text
GET|HEAD /s/<64 位小写十六进制 Token>/profile.yaml
```

响应为原始 YAML 字节，带 `Cache-Control: private, no-store, max-age=0`、`Referrer-Policy: no-referrer`、`X-Content-Type-Options: nosniff`，不返回 ETag，忽略条件请求和 Range 请求头，始终返回完整内容。其他路径、查询参数、编码变体和方法一律返回 404。停用、重置 Token 或删除后，旧路径同样返回 404。
