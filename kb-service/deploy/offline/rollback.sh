#!/usr/bin/env bash
# 能小智离线回滚（Linux，任务 5.2）
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INSTALL_DIR=${INSTALL_DIR:-/opt/nengxiaozhi}
TO_RELEASE=${TO_RELEASE:-}
RESTORE_DB=${RESTORE_DB:-}
NO_START=${NO_START:-0}
[[ ${1:-} == "-h" || ${1:-} == "--help" ]] && { echo "用法：TO_RELEASE=<目录名> RESTORE_DB=<dump> rollback.sh"; exit 0; }

. "$SCRIPT_DIR/common.sh"
ROOT=$(cd "$INSTALL_DIR" && pwd)
CURRENT=$(current_release "$ROOT")
[[ -n "$CURRENT" ]] || die "未检测到已安装版本"
if [[ -z "$TO_RELEASE" ]]; then
    TO_RELEASE=$(ls -1 "$ROOT/releases" | grep -v "^$(basename "$CURRENT")$" | sort | tail -n1 || true)
    [[ -n "$TO_RELEASE" ]] || die "没有可回滚的历史版本"
fi
TARGET="$ROOT/releases/$TO_RELEASE"
[[ -d "$TARGET" ]] || die "目标版本不存在：$TARGET"
[[ "$TARGET" != "$CURRENT" ]] || die "目标版本与当前版本相同"

log "当前版本：$(basename "$CURRENT")"
log "回滚目标：$TO_RELEASE"
stop_service "$ROOT"
if [[ -n "$RESTORE_DB" ]]; then
    [[ -f "$RESTORE_DB" ]] || die "数据库备份不存在：$RESTORE_DB"
    warn "将覆盖当前数据库：$RESTORE_DB"
    source "$ROOT/shared/.env"
    MYSQL_PWD="${DB_PASSWORD:-}" docker exec -i -e MYSQL_PWD nengxiaozhi-mysql \
        mysql -u"${DB_USER:-root}" "${DB_NAME:-nengxiaozhi}" < "$RESTORE_DB"
fi
switch_current "$ROOT" "$TARGET"
[[ "$NO_START" == "0" ]] && start_service "$ROOT"
warn "数据库迁移不回退：新版本新增表/列会保留；精确回退请用 RESTORE_DB 指定升级前备份。"
log "回滚完成：$TO_RELEASE"
