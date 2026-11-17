#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ "$(id -u)" -ne 0 ]; then
    echo '请使用 sudo sh scripts/init-data.sh（只修改本项目 data 目录权限）' >&2
    exit 1
fi
for path in data data/db data/private data/published; do
    if [ -L "$path" ]; then echo '拒绝符号链接数据目录' >&2; exit 1; fi
done
mkdir -p data/db data/private data/published
# Bind mounts are project-owned. Never follow symlinks while changing ownership.
find data -type l -print -quit | while IFS= read -r link; do
    echo '数据目录内存在符号链接，请先检查后移除' >&2
    exit 1
done
chown -R 10001:10001 data
chmod 700 data data/db data/private
find data/db data/private -type d -exec chmod 700 {} +
find data/db data/private -type f -exec chmod 600 {} +
chmod 755 data/published
find data/published -type f -exec chmod 644 {} +
echo '数据目录已初始化：admin UID 10001；published 仅允许其他用户读取。'
