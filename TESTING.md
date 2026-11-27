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

## 未覆盖的范围

以下内容无法由仓库内测试验证，需要部署者在目标环境验收：

- 真实 Docker 容器中的交互式恢复与中断续作（`backup.sh` 的控制流测试使用模拟 Docker）。
- Cloudflare Access 身份策略、Tunnel、Cache Bypass 与真实客户端的更新和撤销。
- 宿主机端口从外部不可达、目标平台的资源限制与实际镜像系统包。

固定依赖与镜像不代表持续不存在已知漏洞；发布前应另外检查当前漏洞数据库和实际镜像系统包。

## 最近验证记录

| 日期 | 环境 | 检查 | 结果 |
|---|---|---|---|
| 2026-11-18 | macOS，Python 3.14，0.3.1 版本号更新后 | 全量 Python 测试；恢复相关测试另以 dash 运行；Shell / JS 语法；发布检查与源码打包 | 通过；158 passed、1 skipped（RLIMIT_AS）；dash 下 61 passed；70 个文件无规则命中 |
| 2026-11-18 | GitHub Actions，`main` 提交 `0a9d283`（与 0.3.1 仅差版本号） | `source-and-tests` / `containers` | 通过；159 passed；容器内 155 passed，HTTP 6 passed |
| 2026-11-18 | GitHub Actions，`main` 提交 `f71f175` | `source-and-tests`：发布检查、全量 Python 测试、语法与依赖检查、源码打包 | 通过；156 passed（Linux 上含 RLIMIT_AS 测试） |
| 2026-11-18 | GitHub Actions，`main` 提交 `f71f175` | `containers`：容器内 Python 测试、真实 HTTP 用例、Nginx 配置、公开日志为空 | 通过；容器内 152 passed，HTTP 6 passed |
| 2026-11-17 | macOS，本机 Nginx 1.30.6 | `tests.run_native` 真实 HTTP | 配置检查通过；6 passed |

测试输出保留一条 Starlette 测试客户端的 httpx 弃用警告。
