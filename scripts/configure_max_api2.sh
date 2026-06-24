#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
MAX_API_BASE_URL_VALUE="https://platform-api2.max.ru"
REQUESTS_CA_BUNDLE_VALUE="/etc/ssl/certs/ca-certificates.crt"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Файл настроек не найден: $ENV_FILE"
  exit 2
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
backup_file="${ENV_FILE}.backup_${timestamp}_$$"
cp -a "$ENV_FILE" "$backup_file"

temp_file="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
trap 'rm -f "$temp_file"' EXIT

awk -v base_url="$MAX_API_BASE_URL_VALUE" -v ca_bundle="$REQUESTS_CA_BUNDLE_VALUE" '
BEGIN { seen_base = 0; seen_bundle = 0 }
/^[[:space:]]*MAX_API_BASE_URL=/ {
  print "MAX_API_BASE_URL=" base_url
  seen_base = 1
  next
}
/^[[:space:]]*REQUESTS_CA_BUNDLE=/ {
  print "REQUESTS_CA_BUNDLE=" ca_bundle
  seen_bundle = 1
  next
}
{ print }
END {
  if (!seen_base) print "MAX_API_BASE_URL=" base_url
  if (!seen_bundle) print "REQUESTS_CA_BUNDLE=" ca_bundle
}
' "$ENV_FILE" > "$temp_file"

chmod --reference="$ENV_FILE" "$temp_file"
chown --reference="$ENV_FILE" "$temp_file" 2>/dev/null || true
mv "$temp_file" "$ENV_FILE"
trap - EXIT

echo "Настройки MAX API2 обновлены."
echo "Резервная копия настроек: $backup_file"
