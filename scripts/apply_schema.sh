#!/usr/bin/env bash
# Applies db/*.sql migrations in order against jarvis-db.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a

for f in db/*.sql; do
  echo "Applying $f..."
  PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f "$f"
done

echo "Done. Tables:"
PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c '\dt'
