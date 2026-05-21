#!/usr/bin/env bash
set -euo pipefail

APP_DIR_DEFAULT="/opt/max-hr-recruitment-bot"
REPO_URL_DEFAULT="https://github.com/Ilmet90/max-hr-recruitment-bot.git"
BRANCH_DEFAULT="main"
ADMIN_SERVICE_DEFAULT="max-hr-admin.service"
BOT_SERVICE_DEFAULT="max-hr-bot.service"
TMP_DIR="/tmp/max-hr-recruitment-bot-update"
STAMP="$(date +%Y%m%d_%H%M%S)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ "$APP_DIR" != "$APP_DIR_DEFAULT" && -d "$APP_DIR_DEFAULT" ]]; then
  APP_DIR="$APP_DIR_DEFAULT"
fi

REPO_URL="$REPO_URL_DEFAULT"
BRANCH="$BRANCH_DEFAULT"
ADMIN_SERVICE="$ADMIN_SERVICE_DEFAULT"
BOT_SERVICE="$BOT_SERVICE_DEFAULT"
LOG_FILE="$APP_DIR/logs/update.log"

mkdir -p "$APP_DIR/logs" "$APP_DIR/backups"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[$(date '+%F %T')] Начато обновление из GitHub"
cd "$APP_DIR"

if SETTINGS_OUTPUT="$("$APP_DIR/.venv/bin/python" - <<'PY' 2>/dev/null
from app.db import (
    init_db,
    get_admin_service_name,
    get_bot_service_name,
    get_github_branch,
    get_github_repo_url,
    get_install_path,
)

init_db()
print(get_install_path())
print(get_github_repo_url())
print(get_github_branch())
print(get_admin_service_name())
print(get_bot_service_name())
PY
)"; then
  APP_DIR_FROM_DB="$(printf '%s\n' "$SETTINGS_OUTPUT" | sed -n '1p')"
  REPO_URL="$(printf '%s\n' "$SETTINGS_OUTPUT" | sed -n '2p')"
  BRANCH="$(printf '%s\n' "$SETTINGS_OUTPUT" | sed -n '3p')"
  ADMIN_SERVICE="$(printf '%s\n' "$SETTINGS_OUTPUT" | sed -n '4p')"
  BOT_SERVICE="$(printf '%s\n' "$SETTINGS_OUTPUT" | sed -n '5p')"
  APP_DIR="${APP_DIR_FROM_DB:-$APP_DIR}"
  REPO_URL="${REPO_URL:-$REPO_URL_DEFAULT}"
  BRANCH="${BRANCH:-$BRANCH_DEFAULT}"
  ADMIN_SERVICE="${ADMIN_SERVICE:-$ADMIN_SERVICE_DEFAULT}"
  BOT_SERVICE="${BOT_SERVICE:-$BOT_SERVICE_DEFAULT}"
else
  echo "Не удалось прочитать настройки обслуживания из базы, используются значения по умолчанию"
fi

cd "$APP_DIR"
mkdir -p "$APP_DIR/logs" "$APP_DIR/backups"

echo "Остановка служб"
sudo -n systemctl stop "$BOT_SERVICE" || true
sudo -n systemctl stop "$ADMIN_SERVICE" || true

echo "Резервное копирование"
if [[ -f "$APP_DIR/data/bot.sqlite3" ]]; then
  cp "$APP_DIR/data/bot.sqlite3" "$APP_DIR/backups/bot_before_update_$STAMP.sqlite3"
fi
if [[ -d "$APP_DIR/app/static/uploads" ]]; then
  tar -czf "$APP_DIR/backups/uploads_before_update_$STAMP.tar.gz" -C "$APP_DIR" app/static/uploads
fi

echo "Загрузка репозитория"
rm -rf "$TMP_DIR"
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TMP_DIR"

echo "Синхронизация кода"
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.env' \
  --exclude='.venv/' \
  --exclude='data/' \
  --exclude='logs/' \
  --exclude='backups/' \
  --exclude='app/static/uploads/' \
  "$TMP_DIR/" "$APP_DIR/"

echo "Установка зависимостей"
source "$APP_DIR/.venv/bin/activate"
pip install -r requirements.txt

echo "Миграции базы"
python -c "from app.db import init_db; init_db(); print('db ok')"

REMOTE_COMMIT="$(git -C "$TMP_DIR" rev-parse HEAD)"
python - "$REMOTE_COMMIT" <<'PY'
import sys
from app.db import init_db, set_installed_commit

init_db()
set_installed_commit(sys.argv[1])
print("installed_commit saved")
PY

echo "Запуск служб"
sudo -n systemctl start "$ADMIN_SERVICE"
sudo -n systemctl start "$BOT_SERVICE"

rm -rf "$TMP_DIR"
echo "[$(date '+%F %T')] Обновление завершено"
