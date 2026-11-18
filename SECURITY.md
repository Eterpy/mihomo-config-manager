# 安全政策

## 报告问题

请勿在公开 issue 中粘贴真实订阅链接、节点 YAML、Access JWT、备份密码、环境文件或完整数据库。

若仓库启用了 GitHub Private Vulnerability Reporting，请在仓库 **Security → Report a vulnerability** 私下提交复现步骤、影响范围、版本及脱敏样例。若没有私密入口，先开一个不含漏洞细节或敏感数据的 issue，请维护者提供私密联系方式。本仓库不预设私人邮箱。

维护者应在开放仓库时启用私密漏洞报告；操作见 [GitHub 官方文档](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository)。

## 支持范围

安全修复面向最新发布版本，不承诺长期维护历史版本。已知修复与限制见 [安全复审记录](SECURITY_REVIEW.md) 和 [变更记录](CHANGELOG.md)。

## 安全边界

- Access 允许的所有身份都能管理同一配置库；不提供多租户隔离。
- 持有有效订阅 URL 就能读取对应 YAML。Cloudflare、客户端、浏览器及剪贴板工具可能保存该 URL。
- 管理和下载域名必须分开，源站只通过受控的 Tunnel 暴露；下载缓存需要部署者正确配置。
- 数据库存储 Token，私有版本包含原始节点凭据。在线数据没有静态加密；加密备份也需要单独保管密码。
- 宿主机 root、Docker 管理员和有权读取数据的人处于信任边界内。
- 已经下载的内容不能撤回。撤销操作失败时不能视为完成；必要时先停止 public。
- 恢复会换新 Token，但会恢复归档中的启停状态及配置集合；还需重新执行备份之后的停用或删除。
- 迁移到新机器后，旧实例上的 Token 仍然有效；必须停止旧实例并移除其 Tunnel 路由，见 [备份与恢复](docs/backup-restore.md)。
- 备份文件、`data.before-restore` 和恢复暂存目录都包含节点内容；加密备份的安全性取决于密码强度和保管方式。

## 泄露处理

若真实凭据曾提交或分享，先撤销订阅 Token、轮换受影响的节点或服务凭据，再处理 Git 历史、构建产物和缓存。仅删除文件或加入 `.gitignore` 无法清除历史及他人的副本。恢复旧数据前阅读 [备份与恢复](docs/backup-restore.md)。

源码检查是针对常见模式的辅助检查，不是完整秘密扫描或独立安全审计。不能据此承诺不存在所有漏洞或所有形式的敏感信息。
