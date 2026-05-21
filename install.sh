#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/max-hr-recruitment-bot"
SERVICE_USER="maxhrbot"
ADMIN_SERVICE="max-hr-admin.service"
BOT_SERVICE="max-hr-bot.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите установку от root: sudo bash install.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

read_with_default() {
  local prompt="$1"
  local default="$2"
  local value
  read -r -p "$prompt [$default]: " value
  printf '%s' "${value:-$default}"
}

read_secret() {
  local prompt="$1"
  local value
  read -r -s -p "$prompt: " value
  echo
  printf '%s' "$value"
}

dotenv_escape() {
  local value="${1//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

write_env_line() {
  local key="$1"
  local value="$2"
  printf '%s=%s\n' "$key" "$(dotenv_escape "$value")"
}

echo "== MAX HR Recruitment Bot installer =="

ORG_FULL_NAME="$(read_with_default "Полное название организации" "Название организации")"
ORG_SHORT_NAME="$(read_with_default "Краткое название организации" "Организация")"
PARENT_ORG="$(read_with_default "Вышестоящая организация" "Вышестоящая организация")"
ORG_REGION="$(read_with_default "Регион" "Регион")"
BOT_DISPLAY_NAME="$(read_with_default "Название бота" "Кадровый чат-бот")"
WEB_LOGIN="$(read_with_default "Логин главной web-учётной записи" "admin")"
WEB_PASSWORD="$(read_secret "Пароль главной web-учётной записи (оставьте пустым для генерации)")"
PASSWORD_GENERATED=0
if [[ -z "$WEB_PASSWORD" ]]; then
  WEB_PASSWORD="$(openssl rand -hex 8)"
  PASSWORD_GENERATED=1
fi
ADMIN_SECRET="$(read_secret "Служебный код для команды /admin (оставьте пустым для генерации)")"
if [[ -z "$ADMIN_SECRET" ]]; then
  ADMIN_SECRET="$(openssl rand -hex 4)"
fi
MAX_BOT_TOKEN="$(read_secret "MAX_BOT_TOKEN (можно оставить пустым)")"
APP_HOST="$(read_with_default "APP_HOST" "0.0.0.0")"
APP_PORT="$(read_with_default "APP_PORT" "8000")"

echo "== Установка системных пакетов =="
apt-get update
apt-get install -y python3 python3-venv python3-pip git curl rsync sqlite3 ca-certificates openssl

echo "== Подготовка пользователя и каталога =="
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$INSTALL_DIR"

echo "== Копирование проекта =="
tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='.env' \
  --exclude='data' \
  --exclude='logs' \
  --exclude='backups' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.sqlite3' \
  --exclude='*.db' \
  --exclude='app/static/uploads/*' \
  -C "$SCRIPT_DIR" -cf - . | tar -C "$INSTALL_DIR" -xf -

mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs" "$INSTALL_DIR/app/static/uploads/service"
touch "$INSTALL_DIR/app/static/uploads/.gitkeep" "$INSTALL_DIR/app/static/uploads/service/.gitkeep"

echo "== Создание виртуального окружения =="
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo "== Создание .env =="
{
  write_env_line "MAX_BOT_TOKEN" "$MAX_BOT_TOKEN"
  write_env_line "ADMIN_SECRET" "$ADMIN_SECRET"
  write_env_line "WEB_ADMIN_LOGIN" "$WEB_LOGIN"
  write_env_line "WEB_ADMIN_PASSWORD" "$WEB_PASSWORD"
  write_env_line "APP_HOST" "$APP_HOST"
  write_env_line "APP_PORT" "$APP_PORT"
  write_env_line "DATABASE_PATH" "data/bot.sqlite3"
  write_env_line "UPLOAD_DIR" "app/static/uploads"
} > "$INSTALL_DIR/.env"
chmod 600 "$INSTALL_DIR/.env"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "== Инициализация базы данных =="
runuser -u "$SERVICE_USER" -- env \
  ORG_FULL_NAME="$ORG_FULL_NAME" \
  ORG_SHORT_NAME="$ORG_SHORT_NAME" \
  PARENT_ORG="$PARENT_ORG" \
  ORG_REGION="$ORG_REGION" \
  BOT_DISPLAY_NAME="$BOT_DISPLAY_NAME" \
  "$INSTALL_DIR/.venv/bin/python" - <<'PY'
import os
from app.db import init_db, set_setting, update_org_settings

init_db()
update_org_settings(
    {
        "organization_full_name": os.environ["ORG_FULL_NAME"],
        "organization_short_name": os.environ["ORG_SHORT_NAME"],
        "parent_organization": os.environ["PARENT_ORG"],
        "organization_region": os.environ["ORG_REGION"],
        "bot_display_name": os.environ["BOT_DISPLAY_NAME"],
        "community_name": os.environ["ORG_FULL_NAME"],
        "web_admin_title": "Панель управления кадровым чат-ботом",
        "web_admin_header": "Панель управления кадровым чат-ботом",
    },
    None,
    "Установщик",
)
set_setting("admin_service_name", "max-hr-admin.service")
set_setting("bot_service_name", "max-hr-bot.service")
set_setting("install_path", "/opt/max-hr-recruitment-bot")
print("db ok")
PY

echo "== Создание systemd-служб =="
cat > "/etc/systemd/system/$ADMIN_SERVICE" <<EOF
[Unit]
Description=MAX HR Recruitment Bot web admin
After=network.target

[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m app.main
User=$SERVICE_USER
Group=$SERVICE_USER
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > "/etc/systemd/system/$BOT_SERVICE" <<EOF
[Unit]
Description=MAX HR Recruitment Bot polling worker
After=network.target

[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m app.bot
User=$SERVICE_USER
Group=$SERVICE_USER
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$ADMIN_SERVICE"

if [[ -n "$MAX_BOT_TOKEN" ]]; then
  systemctl enable --now "$BOT_SERVICE"
else
  echo "MAX_BOT_TOKEN не задан. Служба бота создана, но не запущена."
fi

SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
SERVER_IP="${SERVER_IP:-IP_сервера}"

echo
echo "== Установка завершена =="
echo "Web-админка: http://$SERVER_IP:$APP_PORT"
echo "Логин главной учётной записи: $WEB_LOGIN"
if [[ "$PASSWORD_GENERATED" -eq 1 ]]; then
  echo "Сгенерированный пароль главной учётной записи: $WEB_PASSWORD"
fi
if [[ -z "$MAX_BOT_TOKEN" ]]; then
  echo
  echo "MAX_BOT_TOKEN не задан. После получения токена внесите его в $INSTALL_DIR/.env и выполните:"
  echo "sudo systemctl start $BOT_SERVICE"
  echo "sudo systemctl enable $BOT_SERVICE"
fi
echo
echo "Команды управления:"
echo "sudo systemctl status $ADMIN_SERVICE"
echo "sudo systemctl status $BOT_SERVICE"
echo "sudo systemctl restart $ADMIN_SERVICE"
echo "sudo systemctl restart $BOT_SERVICE"
echo
echo "Для управления обновлениями и перезапуском служб из web-интерфейса выполните один раз:"
echo "sudo bash $INSTALL_DIR/scripts/setup_maintenance_sudoers.sh"
echo
echo "Проект: $INSTALL_DIR"
echo ".env: $INSTALL_DIR/.env"
