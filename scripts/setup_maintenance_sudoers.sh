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
SERVICE_USER="${SERVICE_USER:-maxhrbot}"
ADMIN_SERVICE="${ADMIN_SERVICE:-max-hr-admin.service}"
BOT_SERVICE="${BOT_SERVICE:-max-hr-bot.service}"

cat > "$SUDOERS_FILE" <<EOF
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL start $ADMIN_SERVICE
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL stop $ADMIN_SERVICE
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $ADMIN_SERVICE
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL status $ADMIN_SERVICE
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL start $BOT_SERVICE
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL stop $BOT_SERVICE
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $BOT_SERVICE
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL status $BOT_SERVICE
EOF

chmod 440 "$SUDOERS_FILE"
visudo -c
echo "Sudoers настроен: $SUDOERS_FILE"
