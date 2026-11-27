#!/bin/sh
# Check ownership before stopping or replacing containers.
set -eu
root=$(pwd -P)
members=$(docker compose ps --all --quiet)
running=$(docker ps --no-trunc --quiet)
conflict() {
    echo '另一部署占用项目、数据目录或端口。请核对 docker compose ls --all 并设置 COMPOSE_PROJECT_NAME。' >&2
    exit 1
}
for id in $running; do
    details=$(docker inspect --format '{{println (index .Config.Labels "com.docker.compose.project.working_dir")}}{{range .Mounts}}{{println .Source}}{{end}}{{range .NetworkSettings.Ports}}{{range .}}{{println .HostPort}}{{end}}{{end}}' "$id")
    if printf '%s\n' "$members" | grep -Fx "$id" >/dev/null; then
        owner_dir=$(printf '%s\n' "$details" | sed -n '1p')
        [ "$owner_dir" = "$root" ] || conflict
        continue
    fi
    while IFS= read -r value; do
        case "$value" in "$root"|"$root/data"|"$root/data/"*|18080|18081) conflict;; esac
    done <<EOF
$details
EOF
done
