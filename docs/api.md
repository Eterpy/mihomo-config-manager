# 接口说明

[返回 README](../README.md)。管理 API 与管理页共用同一套认证；公开订阅端只提供只读下载。

## 认证与通用要求

所有管理页面、静态资源、`/openapi.json` 和 `/api/*` 都要求：

- `Host` 与 `ADMIN_HOSTNAME` 完全一致，否则返回 400。
- 请求头 `Cf-Access-Jwt-Assertion` 携带有效的 Cloudflare Access JWT。服务端独立校验 RS256 签名、issuer、audience、有效期和非空的 `sub`，不信任邮箱等身份头。公钥只从 `CF_ACCESS_TEAM_DOMAIN` 获取。

写请求（POST、PUT、PATCH、DELETE）另外要求：

- `Origin` 等于 `https://ADMIN_HOSTNAME`。
- 请求头 `X-CSRF-Token` 与 Cookie `__Host-mcm-csrf` 的值相同且有效。该值由 `GET /` 签发，绑定当前登录身份，有效期 1 小时；admin 重启后全部失效，需要重新打开管理页。

本项目只在浏览器登录场景下验证过。用 Access 服务令牌等非交互方式调用时，需自行确认签发的 JWT 带有非空的 `sub`，否则会被拒绝。

其他限制：

- 上传请求体上限为 `MAX_UPLOAD_MIB` 加 64 KiB，其他请求为 64 KiB。
- 同一时间只处理一个上传，并发上传返回 429，并带 `Retry-After: 2`。
- 备份快照阶段的写请求返回 503。
- 所有响应带 `Cache-Control: private, no-store` 和严格的 CSP；不提供 `/docs`、`/redoc` 交互文档。

## 管理端点

| 方法 | 路径 | 请求 | 成功响应 |
|---|---|---|---|
| GET | `/api/status` | — | `ready`、`max_upload_bytes`、`history_versions`、`snapshotting` |
| GET | `/api/configs` | — | 配置列表 |
| POST | `/api/configs` | multipart：`file`（必填，`.yaml`/`.yml`），可选 `name`（≤100 字符，默认取文件名）、`description`（≤2000 字符） | 201，配置对象 |
| GET | `/api/configs/{id}` | — | 配置对象 |
| GET | `/api/configs/{id}/preview` | — | 当前版本原始内容，`text/plain` |
| PUT | `/api/configs/{id}/content` | multipart 只接受 `file`；可选查询参数 `expected_current_version` 为正整数 | 新版本的配置对象，沿用原订阅 URL；传入版本与当前版本不符时返回 409 |
| PATCH | `/api/configs/{id}` | JSON：`name`、`description`，均可选，不接受其他字段 | 配置对象 |
| POST | `/api/configs/{id}/enable` | — | 配置对象 |
| POST | `/api/configs/{id}/disable` | — | 配置对象 |
| POST | `/api/configs/{id}/rotate-token` | — | 配置对象，带新的订阅 URL |
| DELETE | `/api/configs/{id}` | — | 204，同时删除历史版本 |
| GET | `/api/configs/{id}/versions` | — | 版本列表，新版本在前 |
| GET | `/api/configs/{id}/versions/{version}/preview` | — | 该版本原始内容 |
| POST | `/api/configs/{id}/versions/{version}/rollback` | JSON：`{"expected_current_version": 2}` | 配置对象；当前版本已变化或目标就是当前版本时返回 409 |
| GET | `/api/backups` | — | 备份任务列表 |
| POST | `/api/backups` | JSON：`{"password": "16–256 个字符"}` | 202，备份任务对象 |
| GET | `/api/backups/{id}` | — | 备份任务对象 |
| GET | `/api/backups/{id}/download` | — | 加密备份文件；未完成时返回 409 |
| DELETE | `/api/backups/{id}` | — | 204；任务进行中时返回 409 |

`{id}` 为 UUID，`{version}` 为正整数。不要把真实备份密码写进 shell 历史或脚本。备份 ID 不是下载凭据，下载同样需要登录。

### 返回对象

**配置对象**：`id`、`name`、`description`、`filename`、`size`、`sha256`、`version`、`enabled`、`status`（`ready` / `pending` / `deleting`）、`created_at`、`updated_at`、`subscription_url`。Token 只出现在 `subscription_url` 中。

**版本对象**：`config_id`、`version`、`filename`、`size`、`sha256`、`created_at`、`source_version`（由回滚生成时为来源版本，否则为空）、`is_current`。

**备份任务对象**：`id`、`status`（`queued` / `snapshotting` / `encrypting` / `ready` / `failed`）、`created_at`、`size`、`sha256`、`error_code`。

## 错误

错误统一返回 `{"detail": "说明"}`，不会回显 YAML 内容、密码或 Token。

| 状态码 | 含义 |
|---|---|
| 400 | 请求格式错误或管理域名不匹配 |
| 401 | 缺少或无效的 Access JWT |
| 403 | Origin 或 CSRF 校验失败 |
| 404 | 资源不存在 |
| 409 | 名称冲突、版本已变化、任务状态不允许 |
| 413 | 超过大小限制 |
| 422 | 参数或 YAML 校验失败 |
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
