# 部署指南

[返回 README](../README.md)。以下域名均是示例；用自己的管理域名和订阅域名替换。命令在项目根目录执行。

## 准备主机

使用 Linux、Docker Engine >= 28、Docker Compose v2+，安装 `ss`（通常来自 iproute2）、`flock`（通常来自 util-linux）。需要能拉取固定镜像和 Python 依赖。数据卷使用本地支持 POSIX 权限和文件锁的文件系统。

Docker 旧版本存在 localhost 发布端口可从同一二层网络访问的问题，因此预检拒绝 Engine < 28。也应核查直接路由、额外反向代理和防火墙配置。[Docker 官方说明](https://docs.docker.com/engine/network/port-publishing/)

将源码放到自选目录，例如 `/opt/mihomo-config-manager`。如通过 SSH 传输，使用自己的 SSH 配置；项目不要求特定用户名或端口。已有服务不需要停机或更改路由。

## 配置 Access

在 Cloudflare Zero Trust 中为 `admin.example.com` 创建 Self-hosted Access 应用，Allow 策略仅允许受信任的管理者。所有允许的身份都具有完整管理权限。记录自己的 Team Domain 和应用 AUD。

```sh
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，替换 Team Domain、AUD、管理域名和订阅 HTTPS URL。示例值会被启动校验拒绝。不要用公开 issue 或截图分享完整环境文件。

服务端会独立校验 JWT 的 RS256 签名、issuer、audience、有效期和主体，不信任单独的邮箱头。[Cloudflare JWT 验证说明](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)

## 测试与启动

```sh
sh scripts/test-containers.sh
sudo sh scripts/init-data.sh
sudo sh scripts/preflight.sh
sudo docker compose config --quiet
sudo docker compose up -d --build --wait --wait-timeout 120
sudo docker compose ps
sudo docker compose exec -T public nginx -t
```

测试脚本使用独立 Compose 项目和临时命名卷，不加载生产 `.env`、不发布宿主机端口；退出清理自己的项目和卷。异常强制终止可能留下 `mcm-test-...` 项目，请核对名称后单独清理。

初始化仅修改本项目 `data`：admin UID/GID 为 `10001:10001`，数据库和私有目录 700、文件 600；published 目录 755、文件 644。public UID/GID 为 `101:101`，只读挂载 published。不要使用 `chmod 777`，不要在服务运行时重新初始化权限。

默认端口为管理 `127.0.0.1:18081`、订阅 `127.0.0.1:18080`。若冲突，统一调整 `compose.yaml`、`scripts/preflight.sh`、Tunnel 路由及相关部署测试；保持 loopback 绑定。预检只适用于首次启动，已有项目会占用自己的端口。

```sh
# Host 改为自己配置的管理域名；无 JWT，预期 401。
curl -i -H 'Host: admin.example.com' http://127.0.0.1:18081/api/configs
# 预期 404。
curl -i http://127.0.0.1:18080/
```

## 接入 Tunnel

在宿主机运行 cloudflared，可以新建或复用 Tunnel。按 [Cloudflare 官方指南](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/) 安装并连接；Tunnel 凭据由部署者保管，不放入源码目录。

| 主机名 | Tunnel Service |
|---|---|
| `admin.example.com` | `http://127.0.0.1:18081` |
| `subscribe.example.com` | `http://127.0.0.1:18080` |

管理域名必须先配置 Access，再开放路由。Host Header 保持与 `ADMIN_HOSTNAME` 一致。若 cloudflared 在单独容器内，容器的 loopback 并非宿主机，本指南的地址不能直接沿用；需自行配置网络并重新验证隔离。

订阅域名无需交互登录：避免被通配符 Access 应用或 CAPTCHA/JS Challenge 覆盖。添加 Cache Rule，条件为订阅域名，选择 **Bypass cache**，确保其他规则或 Worker 不覆盖；若该域名曾缓存内容，先清除旧缓存。可参考 [Cloudflare Cache Rules](https://developers.cloudflare.com/cache/how-to/cache-rules/)。

项目生成的链接没有 `?k=` 等全局暗号。不要未经验证添加 URL 重写或缓存规则，否则可能使更新、撤销或客户端导入失效。

## 上线验收

1. 未允许的身份无法进入管理页；允许的身份能够上传、预览和修改配置。
2. 客户端导入订阅 URL 成功；上传新版本后刷新得到新内容。
3. 下载响应包含 `no-store`，Cloudflare 不应返回缓存 HIT；测试同秒更新与条件请求。
4. 停用、重置 Token、删除完成后，真实域名下的旧链接不可读；其他配置不受影响。
5. 从服务器以外检查宿主机 18080/18081 不能直接访问。
6. 使用临时样例演练加密备份、恢复和中断续作；恢复后旧链接不可用，再使用真实数据。

不要把真实 URL 直接写在 shell 历史里。管理访问日志和公开请求日志默认关闭；排障可检查健康状态、HTTP 状态码和 Nginx 语法，不要把请求内容或链接输出到公开日志。
