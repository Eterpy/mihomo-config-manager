#!/bin/sh
# Check ownership before stopping or replacing containers.
set -eu
root=$(pwd -P)
members=$(docker compose ps --all --quiet)
running=$(docker ps --no-trunc --quiet)
conflict() {
    echo '另一部署占用项目、数据目录或端口，或原项目目录已移动或改名。请核对 docker compose ls --all 中的部署路径和 COMPOSE_PROJECT_NAME。' >&2
    exit 1
}
physical_dir() (
    case "$1" in /*) cd -P "$1" 2>/dev/null && pwd -P;; *) return 1;; esac
)
for id in $members; do
    owner_dir=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$id")
    owner_dir=$(physical_dir "$owner_dir") || conflict
    [ "$owner_dir" = "$root" ] || conflict
done
for id in $running; do
    if printf '%s\n' "$members" | grep -Fx "$id" >/dev/null; then continue; fi
    details=$(docker inspect --format '{{println (index .Config.Labels "com.docker.compose.project.working_dir")}}{{range .Mounts}}{{println .Source}}{{end}}{{range .NetworkSettings.Ports}}{{range .}}{{println .HostPort}}{{end}}{{end}}' "$id")
    while IFS= read -r value; do
        case "$value" in "$root"|"$root/data"|"$root/data/"*|18080|18081) conflict;; esac
        if resolved=$(physical_dir "$value"); then
            case "$resolved" in "$root"|"$root/data"|"$root/data/"*) conflict;; esac
        fi
    done <<EOF
$details
EOF
done
