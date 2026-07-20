#!/usr/bin/env sh
set -eu

BASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BACKUP_DIR=${BUGTRACK_BACKUP_DIR:-/var/backups/bugtrack-postgres}
RETENTION_DAYS=${BUGTRACK_BACKUP_RETENTION_DAYS:-14}

cd "$BASE_DIR"
set -a
. ./.env
set +a

mkdir -p "$BACKUP_DIR"
umask 077
STAMP=$(date +%Y%m%d_%H%M%S)
TARGET="$BACKUP_DIR/bugtrack_${STAMP}.dump"

docker exec bugtrack-postgres pg_dump \
  --format=custom \
  --no-owner \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" > "$TARGET"

test -s "$TARGET"
find "$BACKUP_DIR" -type f -name 'bugtrack_*.dump' -mtime "+$RETENTION_DAYS" -delete
printf '%s\n' "$TARGET"

