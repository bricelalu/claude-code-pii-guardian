#!/usr/bin/env bash
# task pii-score: build the 50-row customers table (French open data), export it as CSV,
# Markdown table and minified JSON, and send each export through the running gateway's
# code-guard guardrail (RunPod GLiNER2 + regexes). Reports, per format and per
# column, what was masked and what wasn't. Needs `task up` and a RunPod worker allowed
# (endpoint workers.max >= 1).
# Output: .pii-score-out/{export.*, truth.json, masked/, report.md, results.json}
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT_DIR="$REPO_ROOT/.pii-score-out"
GATEWAY="${GATEWAY_URL:-http://litellm.local:8080}"

for bin in sqlite3 jq python3 curl; do
  command -v "$bin" >/dev/null || { echo "ERROR: $bin is required." >&2; exit 1; }
done
if [ -z "${LITELLM_MASTER_KEY:-}" ] && [ -f "$REPO_ROOT/.env" ]; then
  LITELLM_MASTER_KEY="$(grep -E '^LITELLM_MASTER_KEY=' "$REPO_ROOT/.env" | cut -d= -f2- || true)"
fi
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is not set (env or .env)}"
curl -sf -m 10 "$GATEWAY/health/liveliness" >/dev/null \
  || { echo "ERROR: gateway not reachable at $GATEWAY — run 'task up'." >&2; exit 1; }

rm -rf "$OUT_DIR/masked" "$OUT_DIR"/export.* "$OUT_DIR"/truth.json "$OUT_DIR"/report.md "$OUT_DIR"/results.json
bash "$SCRIPT_DIR/export.sh" "$OUT_DIR"

echo "▶ Sending each export through $GATEWAY/guardrails/apply_guardrail ..."
python3 "$SCRIPT_DIR/gateway_bench.py" --corpus "$OUT_DIR" --out "$OUT_DIR" --gateway "$GATEWAY"

echo ""
echo "Report:       $OUT_DIR/report.md"
echo "Masked files: $OUT_DIR/masked/  (exactly what LiteLLM would send to Anthropic)"
