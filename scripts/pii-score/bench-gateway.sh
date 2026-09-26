#!/usr/bin/env bash
# End-to-end GLiNER2 benchmark through the running gateway (task up must be running and the
# RunPod endpoint must allow at least 1 worker). Each generated source file and each seed
# export goes through LiteLLM's code-guard guardrail; see gateway_bench.py.
# Output: .pii-score-out/gateway-bench/{report.md,results.json,masked/}
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT_DIR="$REPO_ROOT/.pii-score-out/gateway-bench"
GATEWAY="${GATEWAY_URL:-http://litellm.local:8080}"
LANGS="${BENCH_LANGS:-fr,en,es,it}"

for bin in sqlite3 jq python3 curl; do
  command -v "$bin" >/dev/null || { echo "ERROR: $bin is required." >&2; exit 1; }
done
if [ -z "${LITELLM_MASTER_KEY:-}" ] && [ -f "$REPO_ROOT/.env" ]; then
  LITELLM_MASTER_KEY="$(grep -E '^LITELLM_MASTER_KEY=' "$REPO_ROOT/.env" | cut -d= -f2- || true)"
fi
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is not set (env or .env)}"

curl -sf -m 10 "$GATEWAY/health/liveliness" >/dev/null \
  || { echo "ERROR: gateway not reachable at $GATEWAY — run 'task up'." >&2; exit 1; }

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR/exports"
python3 "$REPO_ROOT/bench/corpus/generate.py" --out "$OUT_DIR/corpus" --langs "$LANGS"
bash "$SCRIPT_DIR/export.sh" "$OUT_DIR/exports" >/dev/null
python3 "$SCRIPT_DIR/span_score.py" add-exports --exports "$OUT_DIR/exports" --corpus "$OUT_DIR/corpus"

echo "▶ Sending every file through $GATEWAY/guardrails/apply_guardrail ..."
python3 "$SCRIPT_DIR/gateway_bench.py" --corpus "$OUT_DIR/corpus" \
  --out "$OUT_DIR" --gateway "$GATEWAY"

echo ""
echo "Report:       $OUT_DIR/report.md"
echo "Masked files: $OUT_DIR/masked/  (exactly what LiteLLM would send to Anthropic)"
