# 贡献指南

欢迎提交问题、文档改进与 Pull Request。贡献按本项目 [MIT 许可证](LICENSE) 分发。安全问题请先阅读 [SECURITY.md](SECURITY.md)。

## 开发环境

使用 Python 3.14、Linux 或 macOS；Windows 使用 Linux 环境。无需 Node 构建链。所有命令从仓库根目录执行：

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m pytest -q -m 'not container'
.venv/bin/python -m tests.preview
```

预览数据和签名密钥均临时生成，仅监听本机。测试不得依赖生产 Access、服务器、订阅或密码。不要在测试中加入真实 YAML、固定私钥或可用凭据。

## 提交变更

- 说明解决的问题、行为变化与实际执行的验证；UI 改动可提供仅含样例的截图。
- 涉及认证、订阅撤销、存储、发布或恢复时，补充能复现问题的回归测试。
- 涉及 Docker / Nginx 时运行 `sh scripts/test-containers.sh`；具体覆盖见 [TESTING.md](TESTING.md)。
- 新增发布文件时同步更新 `scripts/package_release.py` 的白名单；运行 `python3 scripts/check_public.py`。
- 暂存文件后运行 `python3 scripts/check_public.py --staged`，检查的是 Git 索引内容，不能用工作区替代。
- 不提交 `.env`、运行目录、备份、私钥、订阅配置、日志、截图里的凭据或个人部署信息。

## 依赖与版本

运行依赖在 `requirements.in`，开发依赖在 `requirements-dev.in`；锁文件包含版本和 SHA-256。使用单独的 Python 3.14 工具环境安装 pip-tools 后重新生成：

```sh
pip-compile --generate-hashes --output-file=requirements.txt requirements.in
pip-compile --generate-hashes --output-file=requirements-dev.txt requirements-dev.in
```

审核两份锁文件并运行测试。基础镜像版本和 digest 需同时更新，生产、测试 Compose 与恢复脚本的应用镜像标签需保持一致。升级依赖不应顺带放松认证或资源限制。

GitHub Actions 检查源码发布、测试和隔离容器。它不读取生产凭据，不自动部署或发布。依赖更新 PR 仍需维护者审核。
