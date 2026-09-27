#!/usr/bin/env bash
# Scenario 3: CARD — a credit card number is masked (not blocked) before reaching Anthropic.
# Blocking was dropped: Claude Code resends the whole conversation every turn, so a blocked card
# would fail every later request of the session until /clear.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-http://litellm.local:8080}"
export ANTHROPIC_AUTH_TOKEN="${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is not set — copy .env.example to .env}"

PROMPT='Help me parse this transaction log: card 4111-1111-1111-1111 charged 89.50 EUR on 2026-05-12.'

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  SCENARIO 3: CARD                                           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "▶ WHAT THE OPERATOR TYPED (contains a credit card number):"
echo "  $PROMPT"
echo ""

START_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "▶ RUNNING AGAINST GATEWAY..."
echo ""

RESPONSE=$(claude -p "$PROMPT" 2>&1) || {
  echo "  ERROR from Claude Code: $RESPONSE"
  exit 1
}

echo "▶ CLAUDE'S RESPONSE (it only saw <CREDIT_CARD>):"
echo "$RESPONSE" | sed 's/^/  /'
echo ""

"$SCRIPT_DIR/show-evidence.sh" "$START_TIME" "CARD — CREDIT_CARD masked"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  VERDICT: ✓ Credit card masked — Anthropic never saw the number,"
echo "  and the session keeps working."
echo "  Check GATEWAY EVIDENCE — <CREDIT_CARD> should appear in the forwarded request body."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
