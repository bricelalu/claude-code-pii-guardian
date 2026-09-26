#!/usr/bin/env bash
# Build the fictional customers table (customers.py) and export it with sqlite3 as CSV,
# Markdown table and minified JSON, plus the answer key (truth.json: every PII cell's span
# and entity). Usage: export.sh <out-dir>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:?usage: export.sh <out-dir>}"
DB="$OUT_DIR/pii_samples.db"
ROWS="SELECT * FROM customers ORDER BY id;"

echo "▶ Building the customers table (50 rows, French open data)..."
mkdir -p "$OUT_DIR"
python3 "$SCRIPT_DIR/customers.py" build --db "$DB"

echo "▶ Exporting the same rows in 3 formats..."
sqlite3 -header -csv "$DB" "$ROWS" > "$OUT_DIR/export.csv"
sqlite3 -markdown    "$DB" "$ROWS" > "$OUT_DIR/export.md"
sqlite3 -json        "$DB" "$ROWS" | jq -c . > "$OUT_DIR/export.json"

# Answer key — never sent to the gateway, used only for scoring.
python3 "$SCRIPT_DIR/customers.py" truth --db "$DB" --dir "$OUT_DIR"
