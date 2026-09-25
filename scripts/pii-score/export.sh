#!/usr/bin/env bash
# Build the fake PII dataset and export identical rows as CSV, Markdown, and
# minified JSON, plus the answer key. Usage: export.sh <out-dir>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:?usage: export.sh <out-dir>}"
DB="$OUT_DIR/pii_samples.db"
ROWS="SELECT id, value FROM pii_samples ORDER BY id;"

echo "▶ Building fake PII dataset..."
rm -f "$DB"
sqlite3 "$DB" < "$SCRIPT_DIR/seed.sql"

echo "▶ Exporting identical rows in 3 formats..."
sqlite3 -header -csv "$DB" "$ROWS" > "$OUT_DIR/export.csv"
sqlite3 -markdown    "$DB" "$ROWS" > "$OUT_DIR/export.md"
sqlite3 -json        "$DB" "$ROWS" | jq -c . > "$OUT_DIR/export.json"

# Answer key — never sent to Presidio, used only by score.py.
sqlite3 -json "$DB" \
  "SELECT id, value, is_pii, expected_entity, category FROM pii_samples ORDER BY id;" \
  | jq -c . > "$OUT_DIR/truth.json"
