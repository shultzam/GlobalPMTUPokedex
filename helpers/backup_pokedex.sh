#!/usr/bin/env bash
# Simple SQLite backup helper for the PMTU Pokedex.
# Creates a timestamped copy of the database and prunes old backups.

set -euo pipefail

DB_PATH="${DB_PATH:-/data/pokedex.db}"
BACKUP_DIR="${BACKUP_DIR:-/home/allen/GlobalPMTUPokedex/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-5}"

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database file not found at $DB_PATH" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

ts="$(date +%Y%m%d-%H%M%S)"
dest="$BACKUP_DIR/pokedex-$ts.db"

# Use SQLite's built-in backup command for a consistent copy, even with WAL.
sqlite3 "$DB_PATH" ".backup '$dest'"
chmod 640 "$dest"

# Keep only the most recent BACKUP_KEEP files.
mapfile -t old_files < <(ls -1t "$BACKUP_DIR"/pokedex-*.db 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))")
if (( ${#old_files[@]} )); then
  rm -- "${old_files[@]}"
fi
