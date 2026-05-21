#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$PROJECT_DIR/backups"
STAMP="$(date +%Y%m%d_%H%M%S)"
ARCHIVE="$BACKUP_DIR/backup_$STAMP.tar.gz"

mkdir -p "$BACKUP_DIR"

tar -czf "$ARCHIVE" \
  -C "$PROJECT_DIR" \
  data/bot.sqlite3 \
  app/static/uploads 2>/dev/null || {
    echo "Не удалось создать архив. Проверьте наличие базы и uploads."
    exit 1
  }

echo "$ARCHIVE"
