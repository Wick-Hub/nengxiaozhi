#!/usr/bin/env bash
set -euo pipefail
case "${1:-start}" in
    start)   systemctl start nengxiaozhi ;;
    stop)    systemctl stop nengxiaozhi ;;
    restart) systemctl restart nengxiaozhi ;;
    status)  systemctl --no-pager --lines=20 status nengxiaozhi ;;
    *) echo "usage: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
