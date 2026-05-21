#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADMIN_SERVICE="max-hr-admin.service"
BOT_SERVICE="max-hr-bot.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите обновление от root: sudo bash scripts/update.sh"
  exit 1
fi

cd "$PROJECT_DIR"

echo "== Остановка служб =="
systemctl stop "$BOT_SERVICE" 2>/dev/null || true
systemctl stop "$ADMIN_SERVICE" 2>/dev/null || true

echo "== Резервная копия =="
bash scripts/backup.sh || true

echo "== Обновление кода =="
if [[ -d .git ]]; then
  git pull --ff-only
else
  echo "Git-репозиторий не настроен. Скопируйте новую версию файлов вручную и запустите этот скрипт повторно."
fi

echo "== Зависимости и миграции =="
.venv/bin/pip install -r requirements.txt
.venv/bin/python -c "from app.db import init_db; init_db(); print('db ok')"

echo "== Запуск служб =="
systemctl start "$ADMIN_SERVICE"
TOKEN_VALUE="$(grep '^MAX_BOT_TOKEN=' .env 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '"')"
if [[ -z "$TOKEN_VALUE" ]]; then
  echo "MAX_BOT_TOKEN не задан, служба бота не запущена."
else
  systemctl start "$BOT_SERVICE" 2>/dev/null || true
fi

echo "Обновление завершено."
