#!/usr/bin/env bash
# Scores Presidio's PII masking/false-positive accuracy across three renderings
# of the same fake dataset: CSV, Markdown table, minified JSON.
#
# Talks directly to Presidio Analyzer/Anonymizer (kubectl port-forward), NOT
# through `claude -p` / LiteLLM / the real Anthropic API like demo/*.sh does.
# That's deliberate: CREDIT_CARD/IBAN_CODE are configured to BLOCK the whole
# request (manifests/21-litellm-config.yaml), which would abort every other
# field in a batched prompt; MASK vs AUDIT are indistinguishable from a
# client-side response; and real API calls are slow, costly, and add LLM
# response noise to what should be a deterministic detection measurement.
# LiteLLM's guardrail is just Presidio's analyze+anonymize called with these
# same thresholds, so testing Presidio directly is a faithful, isolated
# reproduction of the guardrail's actual behavior.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT_DIR="$REPO_ROOT/.pii-score-out"
NAMESPACE="gateway"
ANALYZER_PORT=18081
ANONYMIZER_PORT=18082

for bin in sqlite3 jq python3 kubectl curl; do
  command -v "$bin" >/dev/null || { echo "ERROR: $bin is required." >&2; exit 1; }
done

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

bash "$SCRIPT_DIR/export.sh" "$OUT_DIR"

echo "▶ Port-forwarding Presidio Analyzer/Anonymizer..."
PF_PIDS=()
cleanup() {
  for pid in "${PF_PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT

kubectl port-forward -n "$NAMESPACE" svc/presidio-analyzer "${ANALYZER_PORT}:3000" >/dev/null 2>&1 &
PF_PIDS+=("$!")
kubectl port-forward -n "$NAMESPACE" svc/presidio-anonymizer "${ANONYMIZER_PORT}:3000" >/dev/null 2>&1 &
PF_PIDS+=("$!")

wait_ready() {
  local url="$1" name="$2"
  for _ in $(seq 1 20); do
    curl -sf "$url" >/dev/null 2>&1 && { echo "  ✓ $name ready"; return 0; }
    sleep 0.5
  done
  echo "ERROR: $name did not become ready — is 'task up' running?" >&2
  exit 1
}
wait_ready "http://localhost:${ANALYZER_PORT}/health"   "presidio-analyzer"
wait_ready "http://localhost:${ANONYMIZER_PORT}/health" "presidio-anonymizer"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  PII DETECTION SCORING — CSV vs Markdown vs minified JSON     ║"
echo "╚══════════════════════════════════════════════════════════════╝"

for fmt in csv md json; do
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  FORMAT: ${fmt}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  python3 "$SCRIPT_DIR/score.py" \
    "$OUT_DIR/export.${fmt}" \
    "$OUT_DIR/truth.json" \
    "http://localhost:${ANALYZER_PORT}" \
    "http://localhost:${ANONYMIZER_PORT}" \
    "$OUT_DIR/${fmt}.masked.txt"
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Evidence written to: ${OUT_DIR}"
echo "  *.masked.txt shows exactly what Presidio would send onward for"
echo "  each format — open them to \"see what has been masked\"."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
