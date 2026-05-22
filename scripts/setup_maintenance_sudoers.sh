#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите скрипт через sudo: sudo bash scripts/setup_maintenance_sudoers.sh"
  exit 1
fi

SYSTEMCTL="$(command -v systemctl)"
if [[ -z "$SYSTEMCTL" ]]; then
  echo "systemctl не найден"
  exit 1
fi

SUDOERS_FILE="/etc/sudoers.d/max-hr-maintenance"
APP_USER="${APP_USER:-maxhrbot}"
ADMIN_SERVICE="${ADMIN_SERVICE:-max-hr-admin.service}"
BOT_SERVICE="${BOT_SERVICE:-max-hr-bot.service}"

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
echo "Файл: $SUDOERS_FILE"
