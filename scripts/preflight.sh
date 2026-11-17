#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
command -v docker >/dev/null
docker compose version
docker info --format '{{.OSType}}/{{.Architecture}}'
engine_version=$(docker version --format '{{.Server.Version}}')
engine_major=${engine_version%%.*}
case "$engine_major" in
    ''|*[!0-9]*) echo '无法确认 Docker Engine 版本，停止部署。' >&2; exit 1 ;;
esac
if [ "$engine_major" -lt 28 ]; then
    echo '需要 Docker Engine >= 28：旧版本的 localhost 发布端口可能被同一二层网络主机访问。请先安排兼容性评估；本脚本不会升级或重启 Docker。' >&2
    exit 1
fi
test -f .env || { echo '请先从 .env.example 创建 .env 并填写 Access 配置' >&2; exit 1; }
docker compose config --quiet
if command -v ss >/dev/null 2>&1; then
    if ss -H -ltn '( sport = :18080 or sport = :18081 )' | read -r line; then
        echo '18080 或 18081 已被占用；首次部署停止。升级现有项目请先核对 docker compose ps。' >&2
        exit 1
    fi
else
    echo '缺少 ss，请先检查 18080/18081 的监听占用。' >&2
    exit 1
fi
test -d data/db && test -d data/private && test -d data/published
echo '预检通过；尚未启动或修改任何服务。'
