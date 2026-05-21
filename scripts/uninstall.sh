#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/max-hr-recruitment-bot"
ADMIN_SERVICE="max-hr-admin.service"
BOT_SERVICE="max-hr-bot.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите удаление от root: sudo bash scripts/uninstall.sh"
  exit 1
fi

echo "== Остановка служб =="
systemctl stop "$BOT_SERVICE" 2>/dev/null || true
systemctl stop "$ADMIN_SERVICE" 2>/dev/null || true
systemctl disable "$BOT_SERVICE" 2>/dev/null || true
systemctl disable "$ADMIN_SERVICE" 2>/dev/null || true

rm -f "/etc/systemd/system/$BOT_SERVICE" "/etc/systemd/system/$ADMIN_SERVICE"
systemctl daemon-reload

read -r -p "Удалить каталог $INSTALL_DIR вместе с данными? [y/N]: " answer
case "$answer" in
  y|Y|yes|YES)
    rm -rf "$INSTALL_DIR"
    echo "Каталог проекта удалён."
    ;;
  *)
    echo "Каталог проекта оставлен: $INSTALL_DIR"
    ;;
esac

echo "Удаление служб завершено."
