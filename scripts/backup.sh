#!/bin/sh
# Run on the Linux Docker host, from a terminal. Only this Compose project is stopped.
set -eu
cd "$(dirname "$0")/.."
umask 077
project_dir=$(pwd -P)
# docker --mount splits on commas; a comma could add options such as dropping readonly.
case "$project_dir" in *,*) echo '项目路径不能包含逗号。' >&2; exit 1;; esac
journal() { printf '%s\n' "$journal_prefix$1" > .restore-state.tmp; sync; mv .restore-state.tmp .restore-state; sync; }
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
image=yaml-config-manager-admin:0.4.0
work="$project_dir/.restore-work"
journal_prefix=v2:
old_dir=
owns_work=false
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ "$owns_work" = true ] && [ ! -e .restore-state ]; then
        rm -rf "$work"
        rm -f .restore-state.tmp
        [ "$result" -eq 0 ] || echo '准备未完成，已清理解密暂存，现有数据未改动。' >&2
    elif [ "$result" -ne 0 ] && [ -e .restore-state ]; then
        echo '恢复已中断；请检查状态后运行 sudo sh scripts/backup.sh resume。' >&2
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
if [ "$action" = resume ] && [ ! -f .restore-state ]; then
    echo '没有可续作的恢复记录。若仅有 .restore-work，请核实后移走确认前的暂存数据。' >&2
    exit 1
fi
if [ "$action" != verify ]; then
    [ -f .env ] || { echo '缺少部署文件 .env。' >&2; exit 1; }
    docker compose config --quiet
    sh scripts/check-compose-project.sh
fi
max_mib=${BACKUP_MAX_MIB:-}
if [ -z "$max_mib" ] && [ -f .env ]; then
    max_mib=$(sed -n 's/^BACKUP_MAX_MIB=\([0-9][0-9]*\)$/\1/p' .env | tail -n 1)
fi
max_mib=${max_mib:-1024}
case "$max_mib" in *[!0-9]*) echo 'BACKUP_MAX_MIB 必须为正整数。' >&2; exit 1;; esac
if [ "$action" != resume ]; then
    [ ! -e .restore-state ] && [ ! -e "$work" ] || { echo '存在恢复记录，请先检查并使用 resume；不要覆盖暂存数据。' >&2; exit 1; }
    case "$archive" in /*) ;; *) echo '归档必须使用绝对路径。' >&2; exit 1;; esac
    case "$archive" in *,*) echo '归档路径不能包含逗号。' >&2; exit 1;; esac
    [ -f "$archive" ] && [ ! -L "$archive" ] || exit 1
    old_dir=data.before-restore-$(date -u +%Y%m%dT%H%M%SZ)
    if [ "$action" = restore ] && { [ -e "$old_dir" ] || [ -L "$old_dir" ]; }; then
        echo '本次旧数据目录已存在，请稍后重试。' >&2; exit 1
    fi
    docker image inspect "$image" >/dev/null || { echo '缺少本地管理镜像，请先运行 docker compose build admin。' >&2; exit 1; }
    mkdir -m 700 "$work"
    owns_work=true
    chown 10001:10001 "$work"
    command=verify
    [ "$action" != restore ] || command=prepare
    if ! docker run --rm -it --network none --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --memory 512m --pids-limit 32 --tmpfs /tmp:rw,noexec,nosuid,size=32m --mount "type=bind,src=$archive,dst=/archive,readonly" --mount "type=bind,src=$work,dst=/work" "$image" python -m app.backup_cli "$command" /archive /work/data --max-mib "$max_mib"; then
        exit 1
    fi
    if [ "$action" = verify ]; then exit 0; fi
    printf '%s\n' "$old_dir" > "$work/old-dir"
    # Bind the journal to this preparation, outside the data directory that
    # will be installed. Never regenerate this reference during resume.
    [ -f "$work/data/.restore-proof" ] && [ ! -L "$work/data/.restore-proof" ] || exit 1
    cp "$work/data/.restore-proof" "$work/expected-proof"
    chmod 600 "$work/expected-proof"
    chown 10001:10001 "$work/expected-proof"
    sync
    echo '恢复将停止本项目两容器并重置全部订阅链接。输入 RESTORE 确认：'
    read -r confirmation
    if [ "$confirmation" != RESTORE ]; then exit 1; fi
    # Journal values are fixed states, never shell-sourced paths.
    journal prepared
    sync
fi
[ -f .restore-state ] || exit 1
if [ "$action" = resume ]; then
    docker image inspect "$image" >/dev/null || { echo '缺少本地管理镜像，请先运行 docker compose build admin。' >&2; exit 1; }
fi
state=$(cat .restore-state)
case "$state" in
    v2:*)
        state=${state#v2:}
        [ ! -L "$work/old-dir" ] && [ -f "$work/old-dir" ] || { echo '缺少恢复目录记录。' >&2; exit 1; }
        old_dir=$(cat "$work/old-dir")
        case "$old_dir" in data.before-restore-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;; *) echo '恢复目录记录无效。' >&2; exit 1;; esac ;;
    *) journal_prefix=; old_dir=data.before-restore ;;
esac
[ ! -L "$old_dir" ] || { echo '拒绝符号链接恢复目录。' >&2; exit 1; }
if [ "$state" = prepared ] && [ -e data ] && [ -e "$old_dir" ]; then
    echo '旧数据目录已存在，请先核实并妥善移走。' >&2; exit 1
fi
case "$state" in prepared|old_moved|installed) ;; *) echo '恢复记录无效，停止。' >&2; exit 1;; esac
if [ "$action" = restore ]; then sh scripts/check-compose-project.sh; fi
docker compose down
if [ "$state" = prepared ]; then
    if [ ! -d data ] && [ -d "$old_dir" ] && [ -d "$work/data" ]; then
        journal old_moved; sync; state=old_moved
    fi
fi
if [ "$state" = prepared ]; then
    [ ! -e "$old_dir" ] || { echo '请先妥善移走 data.before-restore，保留它以便人工恢复。' >&2; exit 1; }
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
          mv data "$old_dir"
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
echo "恢复完成。请在管理页复制新订阅链接；旧数据保留在 $old_dir。"
