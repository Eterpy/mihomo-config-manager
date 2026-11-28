# 备份与恢复

[返回 README](../README.md)。本文是备份、恢复与迁移的完整说明。命令在部署目录（下文以 `/opt/yaml-config-manager` 为例）执行；恢复需要 root 和交互终端。

## 机制概览

备份在管理页在线创建，恢复在宿主机上用 `scripts/backup.sh` 离线执行。恢复工具运行在无网络、只读根文件系统、无 capabilities 的一次性容器中，不挂载 Docker socket。

| 备份包含 | 备份不包含 |
|---|---|
| SQLite 数据库快照：配置名称、说明、启停状态、版本记录 | `.env` 与 Cloudflare Access / Tunnel 设置 |
| 每份配置保留的全部私有版本（原始 YAML 字节） | `data/published` 发布文件（恢复后按数据库重新生成） |
| 带大小和 SHA-256 的文件清单 | 服务器上的其他备份、日志 |

- **加密**：密码 16–256 个字符，scrypt 派生密钥，AES-256-GCM 加密并认证整份文件。密码遗失无法恢复，项目不提供找回或明文导出。
- **恢复一定换新全部 Token**。备份记录的是备份时刻的 Token，其中一些可能在备份之后已被重置或撤销；全部换新保证任何已撤销的链接都不会因恢复重新可用。代价是所有客户端都要换成新链接。
- **恢复保留**：配置集合、名称、说明、原始内容、历史版本、启停状态。
- **恢复丢失**：备份之后的全部改动，以及服务器上的备份列表（恢复后从空列表开始）。
- **版本要求**：恢复端只接受与当前程序数据库结构完全一致的备份（当前为 schema 2）。迁移或重装时，两端使用同一版本代码最稳妥。

## 备份策略

项目不提供定时备份，需要按以下时机手动创建：

- **升级、回退或迁移之前**：必须。
- **大批量修改之后**：批量上传、删除或重置 Token 后。
- **定期**：按修改频率，例如每周或每月一次。

存放与保管：

- 每份备份至少下载一份到**服务器之外**。`data/backups` 与业务数据在同一磁盘；同机恢复时它还会随旧数据一起移入 `data.before-restore-<UTC时间戳>`，不能作为唯一副本。
- 密码与备份文件分开保存，例如保存在密码管理器中。不同备份可以使用不同密码。
- 服务器端容量受 `BACKUP_MAX_MIB`（单次上限）和 `BACKUP_QUOTA_MIB`（在盘加密归档总量）限制，见 [部署指南](deployment.md) 的环境变量表。配额按压缩加密后的实际字节计算，未登记的归档也计入已用量。达到配额时不会自动删除旧备份，需在管理页手动删除。
- 建议至少保留最近几份备份，以及每次升级前的那一份；并按「恢复演练」一节定期验证备份确实可以恢复。

## 创建、下载与校验

1. 在管理页底部「加密备份」点击「创建加密备份」，输入并确认密码。
2. 状态依次为「等待处理」「创建快照」「加密中」「已完成」。快照阶段的写操作返回 503，稍后重试即可；公开订阅下载不受影响。
3. 显示「已完成」后点击「下载加密备份」，得到 `备份ID.mcmbackup`。列表中显示的 SHA-256 可用于核对传输是否完整：

   ```sh
   sha256sum 备份ID.mcmbackup      # macOS 使用 shasum -a 256
   ```

4. 「失败」通常是容量配额、磁盘空间不足或任务被重启中断，修正后重新创建即可。服务重启时，未完成的任务会被标记为失败并清理暂存文件，不会自动重试。

列表会报告归档缺失或大小异常；下载前核对 SHA-256。同一份未改动的归档可以复用本进程的校验结果，首次并发续传只需完整读取一次。未登记的普通归档保留并单列，管理员可按文件名核对后手动删除。

离线校验只解密和检查，不停止服务、不改动数据，适合在恢复前或定期确认备份可用：

```sh
sudo sh scripts/backup.sh verify /root/restore/备份ID.mcmbackup
```

校验通过后输出配置数量、版本数量和备份时间，例如 `{"configs": 3, "versions": 7, "created_at": "..."}`。

## 恢复前检查

同机恢复和迁移都先逐项确认：

- [ ] 在 SSH 等**交互终端**中用 `sudo` 执行。cron、管道和 `nohup` 都会被拒绝；备份密码只能在提示时隐藏输入，不能放在参数或环境变量中。
- [ ] 部署目录已有填写完整的 `.env`。脚本会调用 `docker compose`，脚本在解密和停服前通过管理镜像读取实际 Compose 环境，校验应用配置（`resume` 只在尚未停服、`data` 仍在原处时校验；之后的阶段若因配置无法启动 admin，修正 `.env` 后再次 `resume` 即可），并检查同名项目的已停止、运行中容器及其他运行中容器的端口和挂载。
- [ ] 已构建当前代码对应的 admin 镜像：`sudo docker compose build admin`。恢复在该本地镜像中执行，不会从网络拉取。
- [ ] 备份文件使用绝对路径，是普通文件而非符号链接，路径中没有逗号。建议放在部署目录之外，例如 `/root/restore/`。部署目录路径同样不能有逗号。root 所有且权限为 0600 的归档可直接使用。脚本会创建一份 UID 10001 可读的临时密文副本，原件的权限和属主会保留。
- [ ] 核对上次恢复留下的旧数据和可用空间。每次恢复单独保存旧目录；本次目标若已存在，脚本在停服前退出。0.3.x 的固定目录 `data.before-restore` 可与新目录共存。
- [ ] 部署目录中没有 `.restore-state` 或 `.restore-work`；若有，说明上次恢复未完成，先按「中断、续作与故障对照」处理。
- [ ] `.restore-work` 所在文件系统的可用空间须覆盖**解包和重新发布两个阶段中较大的峰值**。解包按“备份文件大小 × 2 ＋ 解包数据量”估算；重新发布按“解包数据量 ＋ 全部已启用配置的当前版本字节数总和”估算。解包数据量包括数据库和全部保留版本；重新发布时 admin 还会在 `published/` 重建每份已启用配置的公开副本。另留 SQLite 日志、文件系统块和目录开销的余量。`.restore-work` 中会同时存在加密归档的临时副本、解密后的压缩包和解压数据。准备完成后会清理临时副本和压缩包；确认前中断会清理暂存目录，确认后中断则保留已准备的数据供 `resume` 续作。
- [ ] `verify` 和 `restore` 优先使用显式传入的 `BACKUP_MAX_MIB`，其次读取部署目录 `.env` 中的单行整数，默认 1024，范围为 1–102400 MiB。需要临时提高时，两条命令都加上同一上限，例如 `sudo BACKUP_MAX_MIB=2048 sh scripts/backup.sh restore ...`。中断后直接运行 `sudo sh scripts/backup.sh resume` 即可，已准备好的数据无需再次解密或传入上限。

## 同机恢复

```sh
cd /opt/yaml-config-manager
sudo sh scripts/backup.sh verify /root/restore/备份ID.mcmbackup
sudo sh scripts/backup.sh restore /root/restore/备份ID.mcmbackup
```

`restore` 的执行顺序：

1. 提示输入备份密码；完整认证并校验归档、清单、数据库结构及版本关系，在暂存目录中换新全部 Token，然后显示配置数量。
2. 提示输入 `RESTORE` 确认。输入其他内容会取消并删除暂存数据，现有服务和数据不受影响。
3. 确认后停止本项目的两个容器，把现有 `data` 移到 `data.before-restore-<UTC时间戳>`。
4. 安装恢复后的数据，校验它确实是本次准备的结果，再重设数据目录权限。
5. 先启动 admin，健康后再启动 public。

确认前取消或收到 HUP、INT、QUIT、PIPE、TERM 时，控制器会先移除本次解密容器，再清理暂存。若无法移除容器或删除明文目录，按脚本提示人工处理。确认后中断则保留恢复记录，使用 `resume` 续作。成功时提示「恢复完成」。之后按「恢复后检查清单」逐项完成。注意，原来位于 `data/backups` 的服务器端备份现在位于脚本输出的时间戳旧目录下的 `backups/`。

## 迁移到新机器

### 1. 旧机器：导出

在管理页创建并下载备份，记下列表中显示的 SHA-256。此时**先不要停止旧机器**，等新机器验收完成再下线。

### 2. 新机器：准备环境

按 [部署指南](deployment.md) 完成到「测试与启动」，使用与旧机器相同版本的代码。先把仓库页面上的克隆地址赋给 shell 变量 `REPO_URL`，然后启动一个空配置库：

```sh
git clone "$REPO_URL" /opt/yaml-config-manager
cd /opt/yaml-config-manager
git checkout v0.4.0                  # 换成旧机器实际运行的版本
cp .env.example .env && chmod 600 .env
# 编辑 .env：填写新机器使用的 Access Team Domain、AUD、管理域名和订阅域名
sudo sh scripts/init-data.sh
sudo sh scripts/preflight.sh
sudo docker compose up -d --build --wait --wait-timeout 120
sudo docker compose ps               # admin 与 public 均为 healthy
```

先启动服务可以在恢复前确认 `.env`、镜像和权限都正确。此时的空数据会在恢复时移到 `data.before-restore-<UTC时间戳>`，验收后删除即可。Tunnel 可以等恢复完成后再配置。

- 新机器可以使用新的域名。订阅链接按 `.env` 中的 `PUBLIC_BASE_URL` 实时生成，恢复后自动使用新域名。
- 管理域名不变时可沿用原 Access 应用；更换管理域名时需要为新域名配置 Access，并填写对应的 AUD。

### 3. 传输并核对

```sh
scp 备份ID.mcmbackup 新机器:/root/restore/
# 在新机器上核对，结果应与管理页显示的 SHA-256 一致
sha256sum /root/restore/备份ID.mcmbackup
```

### 4. 恢复

完成「恢复前检查」后执行：

```sh
sudo sh scripts/backup.sh verify /root/restore/备份ID.mcmbackup
sudo sh scripts/backup.sh restore /root/restore/备份ID.mcmbackup
```

### 5. 切换流量

在新机器上运行 cloudflared，为管理域名和订阅域名配置 Tunnel 路由，并为订阅域名设置 Cache Bypass，然后按 [部署指南](deployment.md) 的「上线验收」检查。

### 6. 下线旧机器

**旧机器上的 Token 不会因为新机器恢复而失效。**只要旧机器仍在运行并能被访问，旧链接就仍然能下载到节点配置。新机器验收通过后：

```sh
cd /opt/yaml-config-manager    # 旧机器上的部署目录
sudo docker compose down
```

然后在 Cloudflare 中删除旧机器的 Tunnel 路由或连接器。确认不再需要回退后，由管理员删除旧机器上的 `data/`、`data.before-restore-<UTC时间戳>/` 和备份文件。

需要两台机器长期同时运行（例如冷备）时，分别使用不同的域名和 Tunnel。两边的 Token 互相独立，在一边重置 Token 不会影响另一边。

## 恢复后检查清单

- [ ] 能登录管理页，配置数量与 `verify` 输出一致。
- [ ] 每张卡片的状态为「已启用」或「已停用」，没有「待恢复」。
- [ ] 逐个复制新订阅链接，更新到所有客户端；旧链接应返回 404。
- [ ] 重新执行备份之后在旧数据上做过的修改：停用、删除、重置 Token 或上传新版本。
- [ ] 恢复后的备份列表为空：立即新建一份备份并下载到服务器之外。
- [ ] 确认无误后删除恢复用的备份副本，以及 `data.before-restore-<UTC时间戳>`。它包含旧 Token 和节点内容，不能打包、上传或公开。
- [ ] 如果是因为凭据泄露而恢复，还要轮换受影响的节点凭据，见 [安全政策](../SECURITY.md)。

## 恢复演练

建议首次上线前和之后定期演练一次，确认备份、密码和流程都可用：

1. 准备**另一台**机器或虚拟机。不要在生产主机上复制一份目录来演练：`compose.yaml` 固定了项目名 `yaml-config-manager`，演练中的 `docker compose down` 会停止生产容器，端口也会冲突。
2. 部署同版本代码，上传几份只含虚构内容的配置（例如 `proxies: []`），各做一次停用、重置 Token 和上传新版本。
3. 创建备份并下载，先执行 `verify`，再执行 `restore`。
4. 再做一次恢复，在输入 `RESTORE` 之后、出现「恢复完成」之前按 Ctrl-C 中断，然后用 `resume` 续作，熟悉中断处理。
5. 按「恢复后检查清单」核对，确认恢复前的订阅链接全部失效。

## 中断、续作与故障对照

恢复进度记录在部署目录的 `.restore-state`，暂存数据在 `.restore-work`。输入 `RESTORE` 后发生中断（断线、磁盘满、admin 启动失败等），先检查磁盘、权限和容器状态，再续作：

```sh
sudo sh scripts/backup.sh resume
```

续作从记录的阶段继续。管理端首次启动前会完整校验安装数据；管理端已启动后，则核对本次安装标识并重新启动服务，保留已提交的管理操作。首次安装校验不通过时公开服务保持停止，旧数据不会被重新发布。不要手动删除或修改 `.restore-state`，也不要把旧 `data` 移回来启动容器。

| 提示或现象 | 原因 | 处理 |
|---|---|---|
| `请用 sudo 执行。` | 未以 root 运行 | 加 `sudo` |
| `需要交互终端。` | 在 cron、管道或 `nohup` 中运行 | 在 SSH 交互会话中执行 |
| `归档必须使用绝对路径。` / `归档路径不能包含逗号。` / `项目路径不能包含逗号。` | 路径不符合要求 | 把文件或部署目录移到符合要求的路径 |
| `拒绝符号链接恢复路径。` | `data` 或恢复相关路径是符号链接 | 换成真实目录后重试 |
| `已有恢复或校验命令运行。` | 另一个 `backup.sh` 正在执行 | 等它结束 |
| `存在恢复记录，请先检查并使用 resume…` | 已有 `.restore-state` 或 `.restore-work` | 有 `.restore-state`：运行 `resume`。只有 `.restore-work`：核对数据目录、容器状态和是否存在活动恢复命令，再妥善移走或清理残留明文 |
| `Unable to find image 'yaml-config-manager-admin:…'` | 没有构建本地镜像 | `sudo docker compose build admin` |
| 提示密码错误或文件被修改 | 认证标签未通过 | 核对密码和备份 SHA-256 |
| 提示大小超限、文件头无效、数据库结构不兼容或暂存不可写 | 相应的容量、格式、版本或磁盘检查失败 | 按提示核对上限、来源、版本和磁盘权限 |
| `env file … .env not found` 一类 Compose 报错 | 缺少 `.env` | 补全 `.env` 后重新运行原命令 |
| 提示本次目标已存在或其他项目占用目录、端口 | 停服前检查发现冲突 | 核对旧数据和 Compose 项目名后再运行 |
| `数据仍被其他进程使用。` | 另一个进程持有数据库写锁 | 用 `docker ps` 找到并停止另一个 admin 实例，再 `resume` |
| `恢复记录无效，停止。` | `.restore-state` 被改动或损坏 | 不要手动修复，保留现场后按下一行处理 |
| `恢复数据与准备阶段不匹配，禁止启动订阅服务。` | 安装的数据不是本次准备的结果，或数据库、发布目录状态异常 | 保持公开服务停止，保留 `data`、`data.before-restore-<UTC时间戳>` 和 `.restore-work`，核实目录状态后从原始加密备份重新准备 |
| `管理端恢复失败，公开服务保持停止。修复后运行 resume。` | admin 健康检查失败 | 查看 `docker compose logs admin`，修正环境变量、权限或磁盘后 `resume` |

若提示存在未 checkpoint 的 WAL，同样保持服务停止，先核查数据库与恢复记录，不要删除 WAL 文件。

## 机制说明（维护者）

### 创建

1. 在存储锁内暂停写入，检查归档大小与临时磁盘空间，用 SQLite 在线备份接口生成数据库快照，复制全部保留的私有版本。
2. 释放存储锁后生成带大小和 SHA-256 的 `manifest.json`，打成 gzip tar 并加密。暂存目录为 `data/backups/.work-<ID>`，完成后原子重命名为 `<ID>.mcmbackup`。

### 格式 v1

8 字节 `MCMBAK01` + 16 字节随机 salt + 12 字节随机 nonce + AES-256-GCM 密文 + 16 字节 tag。前 36 字节作为 AAD；scrypt 固定 N=65536、r=8、p=2，从 UTF-8 密码派生 32 字节密钥。明文为 gzip tar，只含 `manifest.json`、`db/manager.sqlite3` 和清单列出的 `private/<配置ID>/versions/<版本>.yaml`。

### 校验与准备

- 先认证整份文件，再解析归档。解密暂存只在私有目录；解析只接受白名单路径的普通文件，拒绝路径穿越、链接、重复条目和超限展开。
- 清单与实际文件的大小和摘要逐一对应；数据库须通过 `integrity_check` 和 `foreign_key_check`，结构与当前程序完全一致（拒绝额外的触发器、视图或改动过的表），且没有未完成操作。
- 准备阶段为每份配置生成新 Token，确认新 Token 唯一且不在旧集合中；清空备份任务记录，checkpoint WAL，写入恢复指纹 `.restore-proof`，并创建空的发布目录。

### 安装状态

| `.restore-state` | 含义 | `resume` 的下一步 |
|---|---|---|
| （不存在） | 尚未确认，现有数据未改动 | 无法续作 |
| `prepared` | 已确认，旧数据尚未移动 | 停止服务，把 `data` 移到 `data.before-restore-<UTC时间戳>` |
| `old_moved` | 旧数据已移走 | 把 `.restore-work/data` 移为 `data` |
| `installed` | 新数据已就位 | 核验准备指纹，启动 admin 并记录 `started` |
| `started` | 本次安装已经验证，admin 已健康启动 | 核对安装标识，启动 admin 和 public，保留此后的配置修改 |

新恢复记录带 `v2:` 前缀，控制文件 `.restore-work/old-dir` 记录 UTC 目录名。续作兼容旧版无前缀记录及其固定旧目录。安装后首次启动前会运行 `app.restore_guard`：确认 `data/.restore-proof` 与移动目录前固定在数据目录之外的 `.restore-work/expected-proof` 一致，并与数据库中的配置和 Token 指纹一致；没有未完成操作，发布目录只含已启用配置的新 Token 文件。另一轮恢复留下的旧目录即使带有自己的有效指纹也不能通过。不要删除、复制替换或手工伪造这些文件。校验通过后才重设权限并依次启动 admin 和 public，public 启动失败时保留 `started` 记录和已运行的 admin；修复后执行 `resume`。成功后删除 `.restore-state` 与 `.restore-work`。
