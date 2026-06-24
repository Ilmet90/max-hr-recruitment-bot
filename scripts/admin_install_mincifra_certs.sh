#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALLOWED_ROOT="$APP_DIR/data/runtime/cert_uploads"
ROOT_ARCHIVE_NAME="linux_russian_trusted_root_ca_pem.zip"
SUB_ARCHIVE_NAME="russian_trusted_sub_ca_pem.zip"

if [[ $# -ne 1 ]]; then
  echo "Ожидается один приватный каталог загрузки сертификатов."
  exit 2
fi

ALLOWED_ROOT_REAL="$(realpath -m "$ALLOWED_ROOT")"
UPLOAD_DIR_REAL="$(realpath -e "$1" 2>/dev/null || true)"
upload_id="$(basename "$UPLOAD_DIR_REAL")"

if [[ -z "$UPLOAD_DIR_REAL" || "$(dirname "$UPLOAD_DIR_REAL")" != "$ALLOWED_ROOT_REAL" || ! "$upload_id" =~ ^[a-f0-9]{32}$ ]]; then
  echo "Каталог загрузки находится вне разрешённой области."
  exit 3
fi
if (
  [[ ! -f "$UPLOAD_DIR_REAL/$ROOT_ARCHIVE_NAME" ]]
  || [[ ! -f "$UPLOAD_DIR_REAL/$SUB_ARCHIVE_NAME" ]]
  || [[ -L "$UPLOAD_DIR_REAL/$ROOT_ARCHIVE_NAME" ]]
  || [[ -L "$UPLOAD_DIR_REAL/$SUB_ARCHIVE_NAME" ]]
); then
  echo "В каталоге отсутствуют обязательные архивы сертификатов."
  exit 3
fi
for archive in "$UPLOAD_DIR_REAL/$ROOT_ARCHIVE_NAME" "$UPLOAD_DIR_REAL/$SUB_ARCHIVE_NAME"; do
  if [[ $(stat -c %s "$archive") -gt 26214400 ]]; then
    echo "Размер каждого архива не должен превышать 25 МБ."
    exit 3
  fi
done
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Системная установка сертификатов требует запуска через sudo."
  exit 4
fi

"$APP_DIR/scripts/install_mincifra_certs.sh" "$UPLOAD_DIR_REAL"
"$APP_DIR/scripts/configure_max_api2.sh"

if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
  PYTHON="$APP_DIR/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

BOT_SERVICE="$(cd "$APP_DIR" && "$PYTHON" -c 'from dotenv import load_dotenv; load_dotenv(".env"); from app import db; print(db.get_bot_service_name())')"
if [[ ! "$BOT_SERVICE" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
  echo "В настройках указано некорректное имя службы MAX-бота."
  exit 5
fi
SYSTEMCTL="$(command -v systemctl)"
"$SYSTEMCTL" restart "$BOT_SERVICE"

rm -rf "$UPLOAD_DIR_REAL"
echo "Настройка MAX API2 завершена. Служба $BOT_SERVICE перезапущена."
