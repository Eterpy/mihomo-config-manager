# 发布指南

[返回 README](../README.md)。公开源码与运行数据必须分离，源码包不用于备份订阅。

## 生成可分发源码

```sh
python3 scripts/check_public.py
python3 scripts/package_release.py /tmp/mihomo-config-manager-source.tar.gz
```

打包工具只收录指定文件和源码扩展名，拒绝符号链接与硬链接，并在写包前执行常见秘密模式检查。输出独占创建，已有包不会覆盖。归档不包含本地 `.git`、虚拟环境、缓存、`.env`、运行数据和备份；归档成员清除本机所有者与修改时间。

`check_public.py` 默认检查白名单源码和 Git 尚未忽略的文件（有 Git 时），`--staged` 检查索引中全部文件及其实际内容，包括被强制暂存的文件。检查无命中仍需人工审阅域名、账号、注释和截图；它不能检测所有秘密。

## 首次上传到公开 GitHub 仓库

最稳妥的起点是在**全新空目录**解压发布包，然后初始化 Git。这样原工作目录中工具保存的本地快照或历史对象不会进入新仓库。

```sh
mkdir mihomo-config-manager-public
cd mihomo-config-manager-public
tar -xzf /tmp/mihomo-config-manager-source.tar.gz
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

本地工具快照可能保留清理前文字；不要复制旧 `.git`、使用镜像推送，或把整个工作目录压缩上传。如果使用已有 Git 历史，必须另行检查所有待发布提交、分支和标签；当前文件干净不代表历史干净。已暴露凭据须先轮换。

在 GitHub 仓库设置中启用适用的秘密扫描、推送保护、私密漏洞报告，并确认 CI 在目标仓库成功运行。配置入口参考 [GitHub 安全文档](https://docs.github.com/en/code-security)。这些设置不会由源码文件自动启用。

## 后续版本

1. 更新变更记录和应用版本，统一 Compose 与恢复脚本中的应用镜像标签。
2. 更新依赖时重新生成哈希锁文件，并核查基础镜像版本和 digest。
3. 执行源码检查、测试和隔离容器验收；没有执行的检查应明确列出。
4. 审核新文件是否需要进入打包白名单，检查实际归档成员及其内容。
5. 发布源码归档或 Git 标签，不上传数据卷、镜像内运行数据、截图中的订阅链接或生产日志。
