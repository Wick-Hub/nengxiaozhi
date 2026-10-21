#!/usr/bin/env bash
# 能小智离线升级（Linux，任务 5.2）
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BUNDLE_DIR=${BUNDLE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}
INSTALL_DIR=${INSTALL_DIR:-/opt/nengxiaozhi}
PYTHON=${PYTHON:-python3}
SKIP_DB_BACKUP=${SKIP_DB_BACKUP:-0}
SKIP_DB_MIGRATE=${SKIP_DB_MIGRATE:-0}
NO_START=${NO_START:-0}
[[ ${1:-} == "-h" || ${1:-} == "--help" ]] && { echo "用法：BUNDLE_DIR=... INSTALL_DIR=... upgrade.sh"; exit 0; }

. "$SCRIPT_DIR/common.sh"
log "==== 能小智离线升级（Linux）===="
ROOT=$(cd "$INSTALL_DIR" && pwd)
OLD=$(current_release "$ROOT")
[[ -n "$OLD" ]] || die "未检测到已安装版本；首次安装请用 install.sh"
verify_bundle "$BUNDLE_DIR"
[[ "$(bundle_info "$BUNDLE_DIR" includesWheels)" == "True" ]] || die "离线包未包含 wheels"
TARGET_PY=$(bundle_info "$BUNDLE_DIR" targetPython)
"$PYTHON" - "$TARGET_PY" <<'PY' || die "Python 版本不匹配（需要 $TARGET_PY 64 位）"
import struct,sys
want=sys.argv[1]; have="%d.%d" % sys.version_info[:2]
if have!=want or struct.calcsize("P")*8!=64: sys.exit(1)
PY
log "当前版本：$(basename "$OLD")"

STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$ROOT/backups/$STAMP"
mkdir -p "$BACKUP"
cp "$ROOT/shared/.env" "$BACKUP/.env"
printf '%s\n' "$(basename "$OLD")" > "$BACKUP/previous-release.txt"
stop_service "$ROOT"
if [[ "$SKIP_DB_BACKUP" == "0" ]]; then backup_database "$OLD" "$BACKUP"; fi
python3 - "$BACKUP" "$OLD" "$BACKUP_DUMP" <<'PY'
import json,sys,datetime,os
backup,old,dump=sys.argv[1:4]
open(f"{backup}/BACKUP-INFO.json","w",encoding="utf-8").write(json.dumps({
 "at":datetime.datetime.now().isoformat(timespec="seconds"),
 "previousRelease":os.path.basename(old),"databaseDump":dump},ensure_ascii=False,indent=2))
PY

rollback() {
    warn "升级失败，回滚到 $(basename "$OLD") …"
    switch_current "$ROOT" "$OLD"
    [[ "$NO_START" == "0" ]] && start_service "$ROOT" || true
}
new_release "$BUNDLE_DIR" "$ROOT" "$PYTHON" "$ROOT/shared/.env" || { rollback; die "展开新版本失败"; }
NEW=$NEW_RELEASE
switch_current "$ROOT" "$NEW"
if [[ "$SKIP_DB_MIGRATE" == "0" ]]; then
    db_init "$NEW" || { rollback; die "数据库迁移失败"; }
fi
if [[ "$NO_START" == "0" ]]; then
    start_service "$ROOT" || { stop_service "$ROOT" || true; rollback; die "新版本健康检查失败"; }
fi
python3 - "$ROOT" "$NEW" "$BUNDLE_DIR" upgrade <<'PY'
import json,sys,datetime
root,release,bundle,action=sys.argv[1:5]
path=f"{root}/INSTALL-INFO.json"
data=json.load(open(path,encoding="utf-8"))
data.update({"current":release.split("/")[-1],"updatedAt":datetime.datetime.now().isoformat(timespec="seconds")})
data.setdefault("history",[]).append({"action":action,"release":release.split("/")[-1],
 "bundleVersion":json.load(open(f"{bundle}/BUILD-INFO.json",encoding="utf-8"))["version"],
 "at":datetime.datetime.now().isoformat(timespec="seconds")})
open(path,"w",encoding="utf-8").write(json.dumps(data,ensure_ascii=False,indent=2))
PY
log "升级完成：$(basename "$NEW")"
log "备份目录：$BACKUP"
[[ -n "$BACKUP_DUMP" ]] && log "数据库备份：$BACKUP_DUMP"
log "回滚：INSTALL_DIR=$ROOT $SCRIPT_DIR/rollback.sh"
