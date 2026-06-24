#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_ARCHIVE_NAME="linux_russian_trusted_root_ca_pem.zip"
SUB_ARCHIVE_NAME="russian_trusted_sub_ca_pem.zip"
TARGET_DIR="/usr/local/share/ca-certificates/mincifra"

usage() {
  echo "Использование:"
  echo "  sudo $0 /путь/к/каталогу-с-архивами"
  echo "  sudo $0 /путь/$ROOT_ARCHIVE_NAME /путь/$SUB_ARCHIVE_NAME"
}

missing=()
for command_name in python3 unzip openssl update-ca-certificates; do
  command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
done
if ((${#missing[@]})); then
  echo "Не найдены необходимые команды: ${missing[*]}"
  echo "Установите зависимости: sudo apt install -y python3 unzip openssl ca-certificates"
  exit 2
fi

if [[ $# -eq 1 && -d "$1" ]]; then
  ROOT_ARCHIVE="$1/$ROOT_ARCHIVE_NAME"
  SUB_ARCHIVE="$1/$SUB_ARCHIVE_NAME"
elif [[ $# -eq 2 ]]; then
  ROOT_ARCHIVE="$1"
  SUB_ARCHIVE="$2"
else
  usage
  exit 2
fi

if [[ "$(basename "$ROOT_ARCHIVE")" != "$ROOT_ARCHIVE_NAME" || "$(basename "$SUB_ARCHIVE")" != "$SUB_ARCHIVE_NAME" ]]; then
  echo "Ожидаются архивы с точными именами:"
  echo "  $ROOT_ARCHIVE_NAME"
  echo "  $SUB_ARCHIVE_NAME"
  exit 2
fi
if [[ ! -f "$ROOT_ARCHIVE" || ! -f "$SUB_ARCHIVE" ]]; then
  echo "Один или оба архива не найдены."
  exit 2
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Для установки в системное хранилище запустите скрипт через sudo."
  exit 4
fi

TMP_DIR="$(mktemp -d /tmp/max-api2-certs.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT
NORMALIZED_DIR="$TMP_DIR/normalized"
INPUT_DIR="$TMP_DIR/input"
mkdir -p "$NORMALIZED_DIR"
mkdir -p "$INPUT_DIR"
install -m 0600 "$ROOT_ARCHIVE" "$INPUT_DIR/$ROOT_ARCHIVE_NAME"
install -m 0600 "$SUB_ARCHIVE" "$INPUT_DIR/$SUB_ARCHIVE_NAME"
ROOT_ARCHIVE="$INPUT_DIR/$ROOT_ARCHIVE_NAME"
SUB_ARCHIVE="$INPUT_DIR/$SUB_ARCHIVE_NAME"

validate_zip() {
  python3 - "$1" <<'PY'
import re
import stat
import sys
import zipfile
from pathlib import PurePosixPath

archive_path = sys.argv[1]
try:
    archive = zipfile.ZipFile(archive_path)
except (OSError, zipfile.BadZipFile) as exc:
    print(f"Некорректный ZIP-архив: {exc}")
    raise SystemExit(3)

with archive:
    members = archive.infolist()
    if len(members) > 500 or sum(member.file_size for member in members) > 100 * 1024 * 1024:
        print("Архив превышает безопасные ограничения по числу файлов или распакованному размеру.")
        raise SystemExit(3)
    for member in members:
        raw_name = member.filename.replace("\\", "/")
        path = PurePosixPath(raw_name)
        file_type = (member.external_attr >> 16) & 0o170000
        if (
            not raw_name
            or raw_name.startswith("/")
            or ".." in path.parts
            or re.match(r"^[A-Za-z]:", raw_name)
            or file_type == stat.S_IFLNK
        ):
            print(f"Архив содержит небезопасный путь или символическую ссылку: {member.filename}")
            raise SystemExit(3)
PY
}

validate_zip "$ROOT_ARCHIVE"
validate_zip "$SUB_ARCHIVE"

normalize_archive() {
  local archive="$1"
  local prefix="$2"
  local extract_dir="$TMP_DIR/$prefix"
  local count=0
  local source_file
  mkdir -p "$extract_dir"
  unzip -qq "$archive" -d "$extract_dir"
  if find "$extract_dir" -type l -print -quit | grep -q .; then
    echo "Архив $(basename "$archive") содержит символическую ссылку."
    exit 3
  fi
  while IFS= read -r -d '' source_file; do
    if openssl x509 -in "$source_file" -noout >/dev/null 2>&1; then
      count=$((count + 1))
      openssl x509 -in "$source_file" -out "$NORMALIZED_DIR/${prefix}-${count}.crt"
    elif openssl x509 -inform DER -in "$source_file" -noout >/dev/null 2>&1; then
      count=$((count + 1))
      openssl x509 -inform DER -in "$source_file" -out "$NORMALIZED_DIR/${prefix}-${count}.crt"
    else
      echo "Пропущен файл, не являющийся сертификатом X.509: $(basename "$source_file")"
    fi
  done < <(find "$extract_dir" -type f -print0)
  if [[ $count -eq 0 ]]; then
    echo "В архиве $(basename "$archive") не найдено корректных сертификатов X.509."
    exit 3
  fi
  echo "Проверено сертификатов в $(basename "$archive"): $count"
}

normalize_archive "$ROOT_ARCHIVE" "root"
normalize_archive "$SUB_ARCHIVE" "sub"

install -d -m 0755 "$TARGET_DIR"
find "$TARGET_DIR" -maxdepth 1 -type f \( -name 'root-*.crt' -o -name 'sub-*.crt' \) -delete
for certificate in "$NORMALIZED_DIR"/*.crt; do
  install -m 0644 "$certificate" "$TARGET_DIR/$(basename "$certificate")"
done

update-ca-certificates
echo "Сертификаты установлены в $TARGET_DIR."
