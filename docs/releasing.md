# 发布指南

[返回 README](../README.md)。公开源码与运行数据必须分离，源码包不用于备份订阅。

## 生成可分发源码

```sh
python3 scripts/check_public.py
python3 scripts/package_release.py /tmp/yaml-config-manager-source.tar.gz
```

打包工具只收录指定文件和源码扩展名，拒绝符号链接与硬链接，并在写包前执行常见秘密模式检查。输出独占创建，已有包不会覆盖。归档不包含本地 `.git`、虚拟环境、缓存、`.env`、运行数据和备份；归档成员清除本机所有者与修改时间。

不要整目录打包或压缩部署目录：其中的 `data`、`data.before-restore`、`.restore-work` 和环境文件都含 Token 或凭据。源码包也不是数据备份，不能用于恢复订阅。

`check_public.py` 默认检查白名单源码和 Git 尚未忽略的文件（有 Git 时），`--staged` 检查索引中全部文件及其实际内容，包括被强制暂存的文件。检查无命中仍需人工审阅域名、账号、注释和截图；它不能检测所有秘密。

## 首次建立公开仓库

最稳妥的起点是在**全新空目录**解压发布包，然后初始化 Git。这样原工作目录中工具保存的本地快照或历史对象不会进入新仓库。

```sh
mkdir yaml-config-manager-public
cd yaml-config-manager-public
tar -xzf /tmp/yaml-config-manager-source.tar.gz
git init -b main
# 使用自己的署名；如需隐藏邮箱，填写 GitHub 提供的 noreply 邮箱。
git config user.name 'YOUR_PUBLIC_NAME'
git config user.email 'YOUR_GITHUB_NOREPLY_EMAIL'
git add .
python3 scripts/check_public.py --staged
git diff --cached --stat
git diff --cached
```

确认暂存内容只有源码、示例和文档后，提交并推送到自己新建的空仓库。先将 `OWNER` 和 `REPOSITORY` 替换为真实仓库信息：

```sh
git commit -m "Initial open-source release"
git remote add origin https://github.com/OWNER/REPOSITORY.git
git push -u origin main
```

通过 HTTPS 推送时，GitHub 要求访问令牌具备 `workflow` 权限才能写入 `.github/workflows/`；可以使用 `gh auth login` 后执行 `gh auth setup-git`，或为细粒度令牌授予 Contents 与 Workflows 的读写权限。

本地工具快照可能保留清理前文字；不要复制旧 `.git`、使用镜像推送，或把整个工作目录压缩上传。如果使用已有 Git 历史，必须另行检查所有待发布提交、分支和标签；当前文件干净不代表历史干净。已暴露凭据须先轮换。

在 GitHub 仓库设置中启用适用的秘密扫描、推送保护、私密漏洞报告，并确认 CI 在目标仓库成功运行。配置入口参考 [GitHub 安全文档](https://docs.github.com/en/code-security)。这些设置不会由源码文件自动启用。

## 后续版本

1. 在 `CHANGELOG.md` 中把 Unreleased 改为新版本号和日期。
2. 同步修改所有版本号，它们必须一致：

   | 位置 | 内容 |
   |---|---|
   | `app/main.py` | FastAPI 的 `version` |
   | `app/templates/index.html` | 页脚显示的版本 |
   | `compose.yaml` | admin 镜像标签 `yaml-config-manager-admin:<版本>` |
   | `scripts/backup.sh` | 恢复使用的镜像 `image=yaml-config-manager-admin:<版本>` |
   | `compose.test.yaml` | 三处 `yaml-config-manager-test:<版本>` |

   修改后用 `grep -rn '旧版本号' --exclude-dir=.git .` 确认没有遗漏（`CHANGELOG.md` 与 `TESTING.md` 中的历史记录除外）。
3. 更新依赖时重新生成哈希锁文件，并核查基础镜像版本和 digest。
4. 若数据库 schema 或备份格式发生变化，更新 [备份与恢复](backup-restore.md) 中的版本要求，并在变更记录中写明旧备份能否恢复。
5. 执行源码检查、测试和隔离容器验收，在 [测试说明](../TESTING.md) 中更新验证记录；没有执行的检查应明确列出。
6. 新增文件时确认是否需要进入打包白名单，并检查实际归档成员及其内容。
7. 合并到 `main`、确认 CI 通过后打标签并推送：

   ```sh
   git tag -a v<版本> -m v<版本>
   git push origin v<版本>
   ```

8. 可在 GitHub Releases 中基于该标签发布，正文取自变更记录。不要上传数据卷、镜像内运行数据、截图中的订阅链接或生产日志。
