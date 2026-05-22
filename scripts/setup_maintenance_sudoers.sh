#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите скрипт через sudo: sudo bash scripts/setup_maintenance_sudoers.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SYSTEMCTL="$(command -v systemctl)"
if [[ -z "$SYSTEMCTL" ]]; then
  echo "systemctl не найден"
  exit 1
fi

read_setting() {
  local key="$1"
  local default="$2"
  if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    printf '%s\n' "$default"
    return 0
  fi
  (
    cd "$APP_DIR"
    "$APP_DIR/.venv/bin/python" - "$key" "$default" <<'PYCODE'
import sys
from app.db import init_db, get_setting

key = sys.argv[1]
default = sys.argv[2]
try:
    init_db()
    print((get_setting(key, default) or default).strip() or default)
except Exception:
    print(default)
PYCODE
  ) 2>/dev/null
}

ADMIN_SERVICE="${ADMIN_SERVICE:-${1:-}}"
BOT_SERVICE="${BOT_SERVICE:-${2:-}}"
ADMIN_SERVICE="${ADMIN_SERVICE:-$(read_setting "admin_service_name" "max-hr-admin.service")}"
BOT_SERVICE="${BOT_SERVICE:-$(read_setting "bot_service_name" "max-hr-bot.service")}"

APP_USER="${APP_USER:-}"
if [[ -z "$APP_USER" ]]; then
  APP_USER="$("$SYSTEMCTL" show -p User --value "$ADMIN_SERVICE" 2>/dev/null || true)"
fi
if [[ -z "$APP_USER" ]]; then
  if [[ "$ADMIN_SERVICE" == "max-hr-admin.service" ]]; then
    APP_USER="maxhrbot"
  else
    APP_USER="${SUDO_USER:-root}"
    echo "Не удалось определить пользователя службы. Используется: $APP_USER"
  fi
fi

sudoers_name="${ADMIN_SERVICE%.service}-maintenance"
sudoers_name="$(printf '%s' "$sudoers_name" | tr -c 'A-Za-z0-9_.-' '_')"
SUDOERS_FILE="/etc/sudoers.d/$sudoers_name"

cat > "$SUDOERS_FILE" <<EOF
$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL start $ADMIN_SERVICE
$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL stop $ADMIN_SERVICE
$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $ADMIN_SERVICE
$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL status $ADMIN_SERVICE
$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL start $BOT_SERVICE
$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL stop $BOT_SERVICE
$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $BOT_SERVICE
$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL status $BOT_SERVICE
EOF

chmod 440 "$SUDOERS_FILE"
visudo -c
echo "sudoers настроен для пользователя: $APP_USER"
echo "systemctl: $SYSTEMCTL"
echo "Служба web-панели управления: $ADMIN_SERVICE"
echo "Служба MAX-бота: $BOT_SERVICE"
echo "Файл: $SUDOERS_FILE"
