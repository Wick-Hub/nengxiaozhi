#!/usr/bin/env bash
# Install kb-service as a systemd service (task 5.1).
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "run as root: sudo $0 [service-dir]" >&2
    exit 1
fi

SERVICE_DIR=${1:-/opt/nengxiaozhi/kb-service}
SERVICE_USER=${SERVICE_USER:-nengxiaozhi}
APP_PORT=${APP_PORT:-8000}
UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nengxiaozhi.service"
UNIT_DST=/etc/systemd/system/nengxiaozhi.service

if [[ ! -x "${SERVICE_DIR}/.venv/bin/python" ]]; then
    echo "venv not found: ${SERVICE_DIR}/.venv/bin/python" >&2
    exit 1
fi
if [[ ! -f "${SERVICE_DIR}/.env" ]]; then
    echo ".env not found: ${SERVICE_DIR}/.env (copy deploy/env.production.example)" >&2
    exit 1
fi

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${SERVICE_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
    echo "system user created: ${SERVICE_USER}"
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${SERVICE_DIR}/uploads" "${SERVICE_DIR}/logs" 2>/dev/null || true

sed -e "s|__SERVICE_DIR__|${SERVICE_DIR}|g" \
    -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
    -e "s|__APP_PORT__|${APP_PORT}|g" \
    "${UNIT_SRC}" > "${UNIT_DST}"

systemctl daemon-reload
systemctl enable --now nengxiaozhi
systemctl --no-pager --lines=10 status nengxiaozhi
