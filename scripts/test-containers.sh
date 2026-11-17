#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
project="mcm-test-$(date +%s)-$$"
dc() { docker compose -p "$project" -f compose.test.yaml "$@"; }
cleanup() {
    result=$?
    trap - EXIT INT TERM
    if [ "$result" -ne 0 ]; then dc logs --tail=50 admin runner >&2 || true; fi
    dc down --volumes --remove-orphans >/dev/null 2>&1 || true
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
dc config --quiet
dc build init
dc run --rm --no-deps runner python -m pytest -q -m 'not container' -p no:cacheprovider
dc up -d --wait --wait-timeout 120 admin public
dc exec -T public nginx -t
dc run --rm runner
if [ -n "$(dc logs --no-log-prefix public)" ]; then
    echo '公开服务产生了意外日志；请检查脱敏策略。' >&2
    exit 1
fi
echo '容器验收通过；正在清理本次独立项目和测试卷。'
