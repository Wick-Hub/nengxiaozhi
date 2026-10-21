#!/usr/bin/env bash
# 能小智离线安装（Linux，任务 5.2）
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BUNDLE_DIR=${BUNDLE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}
INSTALL_DIR=${INSTALL_DIR:-/opt/nengxiaozhi}
ENV_FILE=${ENV_FILE:-}
PORT=${PORT:-0}
PYTHON=${PYTHON:-python3}
LINK_MODELS=${LINK_MODELS:-0}
SKIP_DB_INIT=${SKIP_DB_INIT:-0}
NO_START=${NO_START:-0}
ALLOW_PLACEHOLDERS=${ALLOW_PLACEHOLDERS:-0}

usage() {
    cat <<'EOF'
用法：install.sh [选项]
  BUNDLE_DIR=/path       离线包目录（默认脚本上级）
  INSTALL_DIR=/opt/...   安装根目录
  ENV_FILE=/path/.env    预填好的生产配置（可选）
  PORT=8000              覆盖 APP_PORT
  LINK_MODELS=1          链接模型（仅同机验收；正式安装请复制）
  SKIP_DB_INIT=1         跳过数据库初始化
  NO_START=1             安装后不启动
  ALLOW_PLACEHOLDERS=1   允许配置占位符（仅测试）
EOF
}
[[ ${1:-} == "-h" || ${1:-} == "--help" ]] && { usage; exit 0; }

. "$SCRIPT_DIR/common.sh"
log "==== 能小智离线安装（Linux）===="
log "离线包：$BUNDLE_DIR"
log "安装目录：$INSTALL_DIR"
verify_bundle "$BUNDLE_DIR"
[[ "$(bundle_info "$BUNDLE_DIR" includesWheels)" == "True" ]] || die "离线包未包含 wheels"

TARGET_PY=$(bundle_info "$BUNDLE_DIR" targetPython)
"$PYTHON" - "$TARGET_PY" <<'PY' || die "Python 版本不匹配（需要 $TARGET_PY 64 位）"
import struct,sys
want=sys.argv[1]
have="%d.%d" % sys.version_info[:2]
if have!=want or struct.calcsize("P")*8!=64:
    print(f"当前 {have} {struct.calcsize('P')*8}bit，期望 {want} 64bit",file=sys.stderr)
    sys.exit(1)
PY

ROOT=$(mkdir -p "$INSTALL_DIR" && cd "$INSTALL_DIR" && pwd)
[[ -z "$(current_release "$ROOT" || true)" ]] || die "检测到已安装版本；升级请用 upgrade.sh"
ensure_shared "$ROOT"
stage_assets "$BUNDLE_DIR" "$ROOT/shared"
if [[ -n "${LINK_MODELS}" && "$LINK_MODELS" != "0" ]]; then
    if [[ -d "$BUNDLE_DIR/payload/models" && -z "$(ls -A "$ROOT/shared/models" 2>/dev/null)" ]]; then
        mkdir -p "$ROOT/shared/models"
        for item in "$BUNDLE_DIR"/payload/models/*; do
            ln -sfn "$item" "$ROOT/shared/models/$(basename "$item")"
        done
        log "模型目录已链接（仅验收）"
    fi
fi

ENV_PATH="$ROOT/shared/.env"
if [[ ! -f "$ENV_PATH" ]]; then
    if [[ -n "$ENV_FILE" ]]; then
        [[ -f "$ENV_FILE" ]] || die "ENV_FILE 不存在：$ENV_FILE"
        cp "$ENV_FILE" "$ENV_PATH"
    else
        cp "$BUNDLE_DIR/payload/kb-service/deploy/env.production.example" "$ENV_PATH"
    fi
fi
[[ "$PORT" != "0" ]] && env_set "$ENV_PATH" APP_PORT "$PORT"
if [[ "$ALLOW_PLACEHOLDERS" == "0" ]]; then check_placeholders "$ENV_PATH"; else
    warn "允许占位符（仅测试）：$(grep -oE '__SET_[A-Z0-9_]+__' "$ENV_PATH" | sort -u | tr '\n' ' ' || true)"
fi
STORE=$(env_get "$ENV_PATH" KB_STORE json)

new_release "$BUNDLE_DIR" "$ROOT" "$PYTHON" "$ENV_PATH"
RELEASE=$NEW_RELEASE
switch_current "$ROOT" "$RELEASE"
python3 - "$ROOT" "$RELEASE" "$BUNDLE_DIR" install <<'PY'
import json,sys,datetime
root,release,bundle,action=sys.argv[1:5]
path=f"{root}/INSTALL-INFO.json"
try: data=json.load(open(path,encoding="utf-8"))
except Exception: data={"history":[]}
data.update({"installDir":root,"current":release.split("/")[-1],
             "updatedAt":datetime.datetime.now().isoformat(timespec="seconds")})
data.setdefault("history",[]).append({"action":action,"release":release.split("/")[-1],
    "bundleVersion":json.load(open(f"{bundle}/BUILD-INFO.json",encoding="utf-8"))["version"],
    "at":datetime.datetime.now().isoformat(timespec="seconds")})
open(path,"w",encoding="utf-8").write(json.dumps(data,ensure_ascii=False,indent=2))
PY

if [[ "$STORE" == "mysql" && "$SKIP_DB_INIT" == "0" ]]; then
    log "初始化数据库（幂等）…"
    db_init "$RELEASE" "$RELEASE/database/schema.sql" 1
else
    log "跳过数据库初始化（KB_STORE=$STORE）"
fi
[[ "$NO_START" == "0" ]] && start_service "$ROOT"
PORT_NOW=$(service_port "$RELEASE/kb-service")
log "安装完成：$RELEASE"
log "应用地址：http://127.0.0.1:$PORT_NOW/"
log "注册 systemd：sudo $RELEASE/kb-service/deploy/linux/install.sh $ROOT/current/kb-service"
