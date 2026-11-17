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

GitHub Actions 配置了上述 Python 和容器任务；仓库实际运行后的 CI 状态才代表该环境验证结果。

## 发布与语法检查

```sh
python3 scripts/check_public.py
# 在 git add 之后检查索引中实际将提交的内容：
python3 scripts/check_public.py --staged
python3 scripts/package_release.py /tmp/mihomo-config-manager-source.tar.gz
node --check app/static/app.js
for script in scripts/*.sh; do sh -n "$script"; done
```

Node 仅用于语法检查，生产不需要。发布检查应与人工审查、Git 历史检查配合，不代表完整的秘密扫描。

## 本次本地验证

日期：2026-11-17。环境：macOS、Python 3.14.0。

| 检查 | 结果 |
|---|---|
| 非容器 Python 全量测试 | 155 passed、1 skipped（Linux RLIMIT_AS）、6 deselected |
| 原生 Nginx 1.30.6 配置与真实 HTTP | 配置通过；6 passed；测试进程与临时数据已清理 |
| 生产 / 测试 Compose 结构 | 独立 Compose 工具验证通过；未启动容器 |
| JS / 所有 Shell 语法、pip check | 通过 |
| 发布检查 | 68 个白名单文件，无内置规则命中；另行核查移除的个人标识、邮箱和非本机 IP |
| 实际源码包与 Git 索引回归 | 文件白名单、归档元数据、秘密模式和强制暂存敏感文件的拒绝测试通过 |
| GitHub YAML / 本地文档链接 | 结构和目标存在性检查通过；未在 GitHub 执行工作流 |

测试保留一条 Starlette 测试客户端的弃用警告。没有 Docker daemon，未执行本次容器构建和交互式容器恢复；没有把此前记录中的漏洞数据库查询当作本次已执行的检查。

目标 Linux 容器、真实 Cloudflare、客户端和交互式恢复仍需部署者验收。固定依赖与镜像不代表持续不存在已知漏洞；发布前应另外检查当前漏洞数据库和实际镜像系统包。

## 0.3.0 公开发布复核（2026-11-18）

- 当前源码重新执行非容器全量测试：155 passed、1 skipped（Linux RLIMIT_AS）、6 deselected，保留同一条测试客户端弃用警告。
- 68 个公开文件通过发布规则检查，另外确认此前移除的个人标识没有重新出现；版本号和本地文档链接一致。
- JS、全部 Shell 语法以及生产 / 测试 Compose 结构检查通过。
- 重新生成与当前源码逐文件一致的 0.3.0 发布目录、ZIP 和 tar 包；在临时新 Git 仓库暂存这 68 个文件后，索引内容检查通过。
- 本轮未重复原生 Nginx HTTP 测试；未运行 Docker 或真实 Cloudflare，未向 GitHub 上传内容。
