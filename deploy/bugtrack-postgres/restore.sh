#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 /path/to/bugtrack_YYYYmmdd_HHMMSS.dump" >&2
  exit 2
fi

BASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BACKUP_FILE=$1
test -s "$BACKUP_FILE"

cd "$BASE_DIR"
set -a
. ./.env
set +a

docker exec -i bugtrack-postgres pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" < "$BACKUP_FILE"

