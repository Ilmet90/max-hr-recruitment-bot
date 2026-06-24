#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
  PYTHON="$APP_DIR/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

"$PYTHON" - <<'PY'
import requests

from app.bot import get_token, token_is_valid
from app.max_api import MaxAPI


token = get_token()
api = MaxAPI(token if token_is_valid(token) else "anonymous-tls-check")
base_url = api.base_url
try:
    response = requests.get(base_url, timeout=20)
    print(f"TLS-соединение с MAX API2 установлено. HTTP-статус без токена: {response.status_code}.")
    if response.status_code == 404:
        print("HTTP 404 без endpoint и токена является ожидаемым ответом и не означает ошибку TLS.")
except requests.exceptions.SSLError as exc:
    print(f"Ошибка проверки TLS-сертификата MAX API2: {exc}")
    raise SystemExit(10)
except requests.exceptions.RequestException as exc:
    print(f"Сетевая ошибка при подключении к MAX API2: {exc}")
    raise SystemExit(11)

if not token_is_valid(token):
    print("MAX_BOT_TOKEN не задан. Авторизованная проверка /me пропущена.")
    raise SystemExit(2)

try:
    api._request("GET", "/me")
    print("Авторизованная проверка MAX API2 /me выполнена успешно.")
except requests.exceptions.SSLError as exc:
    print(f"Ошибка проверки TLS-сертификата при запросе /me: {exc}")
    raise SystemExit(12)
except requests.exceptions.HTTPError as exc:
    status = exc.response.status_code if exc.response is not None else "неизвестен"
    print(f"MAX API2 отклонил авторизацию /me. HTTP-статус: {status}.")
    raise SystemExit(13)
except requests.exceptions.RequestException as exc:
    print(f"Сетевая ошибка при запросе MAX API2 /me: {exc}")
    raise SystemExit(14)
PY
