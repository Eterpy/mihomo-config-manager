#!/bin/sh
# Run on the Linux Docker host, from a terminal. Only this Compose project is stopped.
set -eu
cd "$(dirname "$0")/.."
umask 077
project_dir=$(pwd -P)
# docker --mount splits on commas; a comma could add options such as dropping readonly.
case "$project_dir" in *,*) echo '项目路径不能包含逗号。' >&2; exit 1;; esac
journal() { printf '%s\n' "$1" > .restore-state.tmp; sync; mv .restore-state.tmp .restore-state; sync; }
action=${1:-}
archive=${2:-}
case "$action" in verify|restore|resume) ;; *) echo '用法：sudo sh scripts/backup.sh verify|restore /绝对路径/文件.mcmbackup；中断后使用 resume' >&2; exit 1;; esac
[ "$(id -u)" -eq 0 ] || { echo '请用 sudo 执行。' >&2; exit 1; }
for path in .restore-controller.lock .restore-state.tmp .restore-work .restore-work/expected-proof .restore-state data data.before-restore; do
    [ ! -L "$path" ] || { echo '拒绝符号链接恢复路径。' >&2; exit 1; }
done
exec 8>.restore-controller.lock
flock -n 8 || { echo "已有恢复或校验命令运行。" >&2; exit 1; }
[ -t 0 ] || { echo '需要交互终端。' >&2; exit 1; }
[ ! -L .restore-work ] && [ ! -L .restore-state ] && [ ! -L data ] || exit 1
image=mihomo-config-manager-admin:0.3.1
work="$project_dir/.restore-work"
if [ "$action" != resume ]; then
    [ ! -e .restore-state ] && [ ! -e "$work" ] || { echo '存在恢复记录，请先检查并使用 resume；不要覆盖暂存数据。' >&2; exit 1; }
    case "$archive" in /*) ;; *) echo '归档必须使用绝对路径。' >&2; exit 1;; esac
    case "$archive" in *,*) echo '归档路径不能包含逗号。' >&2; exit 1;; esac
    [ -f "$archive" ] && [ ! -L "$archive" ] || exit 1
    mkdir -m 700 "$work"
    chown 10001:10001 "$work"
    command=verify
    [ "$action" != restore ] || command=prepare
    if ! docker run --rm -it --network none --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --memory 512m --pids-limit 32 --tmpfs /tmp:rw,noexec,nosuid,size=32m --mount "type=bind,src=$archive,dst=/archive,readonly" --mount "type=bind,src=$work,dst=/work" "$image" python -m app.backup_cli "$command" /archive /work/data --max-mib "${BACKUP_MAX_MIB:-1024}"; then
        rm -rf "$work"
        exit 1
    fi
    if [ "$action" = verify ]; then rm -rf "$work"; exit 0; fi
    # Bind the journal to this preparation, outside the data directory that
    # will be installed. Never regenerate this reference during resume.
    [ -f "$work/data/.restore-proof" ] && [ ! -L "$work/data/.restore-proof" ] || exit 1
    cp "$work/data/.restore-proof" "$work/expected-proof"
    chmod 600 "$work/expected-proof"
    chown 10001:10001 "$work/expected-proof"
    sync
    echo '恢复将停止本项目两容器并重置全部订阅链接。输入 RESTORE 确认：'
    read -r confirmation
    if [ "$confirmation" != RESTORE ]; then rm -rf "$work"; exit 1; fi
    # Journal values are fixed states, never shell-sourced paths.
    journal prepared
    sync
fi
[ -f .restore-state ] || exit 1
state=$(cat .restore-state)
case "$state" in prepared|old_moved|installed) ;; *) echo '恢复记录无效，停止。' >&2; exit 1;; esac
docker compose down
if [ "$state" = prepared ]; then
    if [ ! -d data ] && [ -d data.before-restore ] && [ -d "$work/data" ]; then
        journal old_moved; sync; state=old_moved
    fi
fi
if [ "$state" = prepared ]; then
    [ ! -e data.before-restore ] || { echo '请先妥善移走 data.before-restore，保留它以便人工恢复。' >&2; exit 1; }
    if [ -d data ]; then
        # admin creates the writer lock on first start; data initialized but
        # never opened has none. Create it as admin would, so the move is still
        # locked: noclobber refuses existing paths and links, and chown -h never
        # follows one. Ownership matters if this restore is abandoned.
        for path in data/db data/db/.writer.lock; do
            [ ! -L "$path" ] || { echo '拒绝符号链接恢复路径。' >&2; exit 1; }
        done
        if [ ! -d data/db ]; then mkdir data/db; chown -h 10001:10001 data/db; fi
        if [ ! -e data/db/.writer.lock ]; then
            (set -C; : > data/db/.writer.lock)
            chown -h 10001:10001 data/db/.writer.lock
        fi
        (
          flock -n 9 || { echo '数据仍被其他进程使用。' >&2; exit 1; }
          mv data data.before-restore
          sync
        ) 9<data/db/.writer.lock
    fi
    journal old_moved
    sync
    state=old_moved
fi
if [ "$state" = old_moved ]; then
    if [ ! -d data ]; then mv "$work/data" data; fi
    journal installed
    sync
fi
# Never infer that an existing data directory belongs to this restore.
# A stale journal or a manually moved old directory must not revive old tokens.
if ! docker run --rm --network none --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --memory 512m --pids-limit 32 --mount "type=bind,src=$project_dir/data,dst=/data,readonly" --mount "type=bind,src=$work,dst=/restore-control,readonly" "$image" python -m app.restore_guard /data /restore-control/expected-proof; then
    echo '恢复数据与准备阶段不匹配，禁止启动订阅服务。' >&2
    exit 1
fi
sh scripts/init-data.sh
if ! docker compose up -d --wait admin; then
    docker compose stop admin public
    echo '管理端恢复失败，公开服务保持停止。修复后运行 resume。' >&2
    exit 1
fi
if ! docker compose up -d --wait public; then docker compose stop public; exit 1; fi
rm -f .restore-state
rm -rf "$work"
echo '恢复完成。请在管理页复制新订阅链接；旧数据保留在 data.before-restore，不会自动重新发布。'
