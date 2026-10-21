#!/usr/bin/env bash
# 离线交付公共函数（任务 5.2）。由 install/upgrade/rollback 点源引入。
set -euo pipefail

log() { printf '[offline] %s\n' "$*"; }
warn() { printf '[offline] WARN: %s\n' "$*" >&2; }
die() { printf '[offline] ERROR: %s\n' "$*" >&2; exit 1; }
NEW_RELEASE=""
BACKUP_DUMP=""

bundle_info() {
    python3 - "$1/BUILD-INFO.json" "$2" <<'PY'
import json,sys
print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]])
PY
}

verify_bundle() {
    local bundle=$1
    [[ -f "$bundle/SHA256SUMS" ]] || die "缺少 SHA256SUMS：$bundle"
    ( cd "$bundle" && sha256sum -c SHA256SUMS --quiet ) || die "离线包校验失败"
    log "离线包校验通过"
}

env_get() {
    local file=$1 key=$2 default=${3:-}
    [[ -f "$file" ]] || { printf '%s' "$default"; return; }
    local value
    value=$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$file" | head -n1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' || true)
    printf '%s' "${value:-$default}"
}

env_set() {
    local file=$1 key=$2 value=$3
    touch "$file"
    if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$file"; then
        python3 - "$file" "$key" "$value" <<'PY'
import sys
path,key,value=sys.argv[1:4]
lines=open(path,encoding="utf-8").read().splitlines()
out=[]
done=False
for line in lines:
    if not done and line.split("=",1)[0].strip()==key:
        out.append(f"{key}={value}"); done=True
    else:
        out.append(line)
if not done: out.append(f"{key}={value}")
open(path,"w",encoding="utf-8",newline="\n").write("\n".join(out)+"\n")
PY
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

current_release() {
    local root=$1
    [[ -f "$root/current-release.txt" ]] || { printf ''; return; }
    local name
    name=$(tr -d '[:space:]' < "$root/current-release.txt")
    [[ -n "$name" && -d "$root/releases/$name" ]] || die "当前版本目录不存在：$root/releases/$name"
    printf '%s' "$root/releases/$name"
}

switch_current() {
    local root=$1 release=$2
    printf '%s\n' "$(basename "$release")" > "$root/current-release.txt"
    ln -sfn "$release" "$root/current"
}

ensure_shared() {
    local root=$1
    mkdir -p "$root/releases" "$root/shared" "$root/backups" \
        "$root/shared/data" "$root/shared/uploads" "$root/shared/logs" \
        "$root/shared/run" "$root/shared/knowledge-base"
}

stage_assets() {
    local bundle=$1 shared=$2
    if [[ -d "$bundle/payload/knowledge-base" ]] && [[ -z "$(ls -A "$shared/knowledge-base" 2>/dev/null)" ]]; then
        cp -a "$bundle/payload/knowledge-base/." "$shared/knowledge-base/"
    fi
    if [[ -d "$bundle/payload/models" ]] && [[ -z "$(ls -A "$shared/models" 2>/dev/null)" ]]; then
        log "复制模型到共享目录（约 650 MB）…"
        cp -a "$bundle/payload/models" "$shared/models"
    fi
}

new_release() {
    local bundle=$1 root=$2 python=$3 env_file=${4:-}
    local version release app shared wheels venv
    version=$(bundle_info "$bundle" version)
    release="$root/releases/${version}-$(date +%Y%m%d-%H%M%S)"
    [[ -e "$release" ]] && die "版本目录已存在：$release"
    mkdir -p "$release"
    log "展开应用文件到 $release …"
    cp -a "$bundle/payload/kb-service" "$release/kb-service"
    [[ -d "$bundle/payload/database" ]] && cp -a "$bundle/payload/database" "$release/database"
    cp -a "$bundle/BUILD-INFO.json" "$release/BUILD-INFO.json"
    app="$release/kb-service"
    shared="$root/shared"
    for name in data uploads logs run; do
        mkdir -p "$shared/$name"
        ln -sfn "$shared/$name" "$app/$name"
    done
    [[ -d "$shared/models" ]] && ln -sfn "$shared/models" "$app/models"
    [[ -d "$shared/knowledge-base" ]] && ln -sfn "$shared/knowledge-base" "$release/knowledge-base"

    log "创建 venv …"
    "$python" -m venv "$app/.venv"
    venv="$app/.venv/bin/python"
    wheels="$bundle/payload/wheels"
    [[ -d "$wheels" ]] || die "离线包缺少 wheels"
    local requirements="$bundle/scripts/requirements-runtime.txt"
    [[ -f "$requirements" ]] || requirements="$app/deploy/offline/requirements-runtime.txt"
    "$venv" -m pip install --no-index --find-links "$wheels" --upgrade pip setuptools wheel
    "$venv" -m pip install --no-index --find-links "$wheels" -r "$requirements"
    local project_wheel
    project_wheel=$(find "$wheels" -maxdepth 1 -name 'kb_service-*.whl' | head -n1)
    [[ -n "$project_wheel" ]] || die "wheels 中缺少 kb_service-*.whl"
    "$venv" -m pip install --no-index --no-deps "$project_wheel"

    local source=${env_file:-$shared/.env}
    if [[ ! -f "$source" ]]; then
        source="$shared/.env"
        cp "$app/deploy/env.production.example" "$source"
    fi
    cp "$source" "$app/.env"
    env_set "$app/.env" MODEL_DIR "$shared/models"
    env_set "$app/.env" KNOWLEDGE_DIR "../knowledge-base"
    env_set "$app/.env" UPLOAD_DIR "./uploads"
    env_set "$app/.env" STORE_PATH "./data/store.json"
    ( cd "$app" && "$venv" -c "import app.main" ) || die "安装后导入 app.main 失败"
    NEW_RELEASE="$release"
}

check_placeholders() {
    local env_path=$1
    local found
    found=$(grep -oE '__SET_[A-Z0-9_]+__' "$env_path" | sort -u | tr '\n' ' ' || true)
    [[ -z "$found" ]] || die "生产配置仍有占位符：$found（请先填写 $env_path）"
}

db_init() {
    local release=$1 schema=${2:-} seed_auth=${3:-0}
    local app="$release/kb-service"
    if [[ "$(env_get "$app/.env" KB_STORE json)" != "mysql" ]]; then
        log "JSON 模式：跳过数据库初始化"
        return 0
    fi
    local args=(tools/offline_db_init.py)
    [[ -n "$schema" ]] && args+=(--schema "$schema")
    [[ "$seed_auth" == "1" ]] && args+=(--seed-auth)
    ( cd "$app" && ./.venv/bin/python "${args[@]}" )
}

backup_database() {
    local release=$1 backup_dir=$2
    local app="$release/kb-service"
    local store db user pass dump
    BACKUP_DUMP=""
    store=$(env_get "$app/.env" KB_STORE json)
    [[ "$store" == "mysql" ]] || return 0
    db=$(env_get "$app/.env" DB_NAME nengxiaozhi)
    user=$(env_get "$app/.env" DB_USER root)
    pass=$(env_get "$app/.env" DB_PASSWORD '')
    dump="$backup_dir/database.sql"
    if ! command -v docker >/dev/null 2>&1; then warn "未找到 docker，跳过数据库备份"; return 0; fi
    if [[ "$(docker ps --filter name=nengxiaozhi-mysql --format '{{.Names}}' 2>/dev/null)" != "nengxiaozhi-mysql" ]]; then
        warn "容器 nengxiaozhi-mysql 未运行，跳过数据库备份"; return 0
    fi
    log "备份数据库到 $dump …"
    MYSQL_PWD="$pass" docker exec -e MYSQL_PWD nengxiaozhi-mysql \
        mysqldump -u"$user" --single-transaction --routines --databases "$db" > "$dump"
    BACKUP_DUMP="$dump"
}

service_port() {
    local app=$1
    env_get "$app/.env" APP_PORT 8000
}

service_running() {
    local app=$1 pid
    pid=$(cat "$app/run/service.pid" 2>/dev/null || true)
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

start_service() {
    local root=$1 release app port
    release=$(current_release "$root")
    [[ -n "$release" ]] || die "尚未安装任何版本"
    app="$release/kb-service"
    port=$(service_port "$app")
    mkdir -p "$app/run" "$app/logs"
    rm -f "$app/run/stop.flag"
    if service_running "$app"; then log "服务已在运行"; return 0; fi
    nohup "$app/.venv/bin/python" "$app/deploy/service_supervisor.py" --port "$port" \
        > "$app/logs/supervisor.out" 2>&1 &
    log "启动服务（port=$port）…"
    for _ in $(seq 1 30); do
        if curl -fsS "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
            log "健康检查通过"
            return 0
        fi
        sleep 1
    done
    die "健康检查超时"
}

stop_service() {
    local root=$1 release app
    release=$(current_release "$root")
    [[ -n "$release" ]] || return 0
    app="$release/kb-service"
    if ! service_running "$app"; then log "服务未运行"; return 0; fi
    touch "$app/run/stop.flag"
    for _ in $(seq 1 20); do
        service_running "$app" || { log "服务已停止"; return 0; }
        sleep 1
    done
    die "服务停止超时"
}
