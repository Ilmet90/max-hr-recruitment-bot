#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP_DIR="${TMP_DIR:-/tmp/max-hr-recruitment-bot-update}"
REPO_URL_FALLBACK="https://github.com/Ilmet90/max-hr-recruitment-bot.git"
APP_DIR_FALLBACK="$SCRIPT_APP_DIR"
ADMIN_SERVICE_FALLBACK="max-hr-admin.service"
BOT_SERVICE_FALLBACK="max-hr-bot.service"
SYSTEMCTL="$(command -v systemctl || true)"
STAMP="$(date +%Y%m%d_%H%M%S)"
UPDATE_STARTED=0
TARGET_RELEASE="${1:-}"

if [[ -z "$SYSTEMCTL" ]]; then echo "systemctl не найден."; exit 20; fi

read_setting() {
  local base_dir="$1" key="$2" default="$3"
  if [[ ! -x "$base_dir/.venv/bin/python" ]]; then printf '%s\n' "$default"; return 0; fi
  (
    cd "$base_dir"
    "$base_dir/.venv/bin/python" - "$key" "$default" <<'PYCODE'
import sys
from app.db import init_db, get_setting
key, default = sys.argv[1], sys.argv[2]
try:
    init_db()
    print((get_setting(key, default) or default).strip() or default)
except Exception:
    print(default)
PYCODE
  ) 2>/dev/null
}

APP_DIR="${APP_DIR:-}"
if [[ -z "$APP_DIR" ]]; then APP_DIR="$(read_setting "$SCRIPT_APP_DIR" "install_path" "$APP_DIR_FALLBACK")"; fi
APP_DIR="${APP_DIR:-$APP_DIR_FALLBACK}"
REPO_URL="${REPO_URL:-$(read_setting "$APP_DIR" "github_repo_url" "$REPO_URL_FALLBACK") }"; REPO_URL="${REPO_URL% }"
ADMIN_SERVICE="${ADMIN_SERVICE:-$(read_setting "$APP_DIR" "admin_service_name" "$ADMIN_SERVICE_FALLBACK") }"; ADMIN_SERVICE="${ADMIN_SERVICE% }"
BOT_SERVICE="${BOT_SERVICE:-$(read_setting "$APP_DIR" "bot_service_name" "$BOT_SERVICE_FALLBACK") }"; BOT_SERVICE="${BOT_SERVICE% }"

LOG_FILE="$APP_DIR/logs/update.log"
mkdir -p "$APP_DIR/logs" "$APP_DIR/backups"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[$(date '+%F %T')] Начато обновление из опубликованного GitHub Release"
echo "Каталог проекта: $APP_DIR"
echo "Служба web-панели управления: $ADMIN_SERVICE"
echo "Служба MAX-бота: $BOT_SERVICE"

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then echo "Виртуальное окружение приложения не найдено."; exit 21; fi

set +e
RESOLVED_RELEASE="$(
  cd "$APP_DIR"
  "$APP_DIR/.venv/bin/python" - "$REPO_URL" "$TARGET_RELEASE" <<'PYCODE'
import sys
from app.maintenance import get_published_release
release, error = get_published_release(sys.argv[2] or None, sys.argv[1])
if error or not release:
    print(error or "Опубликованный релиз не найден.", file=sys.stderr)
    raise SystemExit(22)
print(release["tag_name"])
PYCODE
)"
RESOLVE_CODE=$?
set -e
if [[ "$RESOLVE_CODE" -ne 0 || -z "$RESOLVED_RELEASE" ]]; then echo "Не удалось подтвердить опубликованный GitHub Release."; exit 22; fi
TARGET_RELEASE="$RESOLVED_RELEASE"
echo "Целевая опубликованная релизная версия: $TARGET_RELEASE"

sudoers_hint() { echo "Управление службами не настроено. Выполните один раз:"; echo "sudo bash $APP_DIR/scripts/setup_maintenance_sudoers.sh"; }
check_sudoers_for_service() {
  local service="$1" allow_not_found="${2:-0}" output="" code=0
  set +e; output="$(sudo -n "$SYSTEMCTL" status "$service" 2>&1 >/dev/null)"; code=$?; set -e
  if [[ "$code" -eq 0 || "$code" -eq 3 || ( "$allow_not_found" -eq 1 && "$code" -eq 4 ) ]]; then return 0; fi
  sudoers_hint; [[ -n "$output" ]] && echo "$output"; return 1
}
bot_token_configured() {
  [[ -f "$APP_DIR/.env" ]] || return 1
  local value
  value="$(grep -E '^MAX_BOT_TOKEN=' "$APP_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '[:space:]' || true)"
  [[ -n "$value" && "$value" != "change-me" && "$value" != "your-token" && "$value" != "MAX_BOT_TOKEN" && "$value" != "put-token-here" ]]
}
restore_services_on_error() {
  local code=$?
  if [[ "$UPDATE_STARTED" -eq 1 ]]; then
    echo "Установка релиза прервана ошибкой. Пытаюсь вернуть службы."
    sudo -n "$SYSTEMCTL" start "$ADMIN_SERVICE" || true
    if bot_token_configured; then sudo -n "$SYSTEMCTL" start "$BOT_SERVICE" || true; fi
  fi
  rm -rf "$TMP_DIR"; exit "$code"
}
trap restore_services_on_error ERR

echo "Проверка прав sudoers"
check_sudoers_for_service "$ADMIN_SERVICE" 0 || exit 20
check_sudoers_for_service "$BOT_SERVICE" 1 || exit 20
cd "$APP_DIR"

echo "Резервное копирование"
if [[ -f "$APP_DIR/data/bot.sqlite3" ]]; then cp "$APP_DIR/data/bot.sqlite3" "$APP_DIR/backups/bot_before_update_$STAMP.sqlite3"; fi
if [[ -d "$APP_DIR/app/static/uploads" ]]; then tar -czf "$APP_DIR/backups/uploads_before_update_$STAMP.tar.gz" -C "$APP_DIR" app/static/uploads; fi

echo "Загрузка опубликованного релиза $TARGET_RELEASE"
rm -rf "$TMP_DIR"
git init -q "$TMP_DIR"
git -C "$TMP_DIR" remote add origin "$REPO_URL"
git -C "$TMP_DIR" fetch --depth 1 origin "refs/tags/$TARGET_RELEASE"
git -C "$TMP_DIR" checkout --detach -q FETCH_HEAD

UPDATE_STARTED=1
echo "Остановка службы MAX-бота"
sudo -n "$SYSTEMCTL" stop "$BOT_SERVICE" || true

echo "Синхронизация кода релиза"
rsync -a --delete \
  --exclude='.git/' --exclude='.env' --exclude='.venv/' --exclude='data/' \
  --exclude='logs/' --exclude='backups/' --exclude='app/static/uploads/' \
  "$TMP_DIR/" "$APP_DIR/"

echo "Установка зависимостей"
source "$APP_DIR/.venv/bin/activate"
pip install -r requirements.txt

echo "Миграции базы"
python -c "from app.db import init_db; init_db(); print('db ok')"
REMOTE_COMMIT="$(git -C "$TMP_DIR" rev-parse HEAD)"
python - "$REMOTE_COMMIT" "$TARGET_RELEASE" <<'PYCODE'
import sys
from app.db import init_db, set_installed_commit, set_setting
init_db()
set_installed_commit(sys.argv[1])
set_setting("installed_release_tag", sys.argv[2])
print("installed release saved")
PYCODE

echo "Запуск службы MAX-бота"
if bot_token_configured; then
  sudo -n "$SYSTEMCTL" restart "$BOT_SERVICE" || echo "MAX_BOT_TOKEN не задан или служба бота не запущена — запуск бота пропущен."
else
  echo "MAX_BOT_TOKEN не задан или служба бота не запущена — запуск бота пропущен."
fi

echo "Плановый перезапуск web-панели управления"
( sleep 2; sudo -n "$SYSTEMCTL" restart "$ADMIN_SERVICE" ) >/dev/null 2>&1 &
rm -rf "$TMP_DIR"
trap - ERR
echo "[$(date '+%F %T')] Релиз $TARGET_RELEASE установлен. Web-панель управления будет перезапущена в самом конце."
