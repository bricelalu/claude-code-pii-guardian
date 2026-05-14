#!/usr/bin/env bash
# Scenario 3: BLOCK — credit card triggers BLOCK; request never leaves the gateway.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="sk-poc-local-only"

PROMPT='Help me parse this transaction log: card 4111-1111-1111-1111 charged 89.50 EUR on 2026-05-12.'

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  SCENARIO 3: BLOCK                                          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "▶ WHAT THE OPERATOR TYPED (contains credit card number):"
echo "  $PROMPT"
echo ""

START_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "▶ RUNNING AGAINST GATEWAY (expecting an error)..."
echo ""

# Capture both stdout and stderr; do not exit on failure — the error IS the evidence.
RESPONSE=$(claude -p "$PROMPT" 2>&1 || true)

echo "▶ CLAUDE CODE RESPONSE (should be an error):"
echo "$RESPONSE" | sed 's/^/  /'
echo ""

"$SCRIPT_DIR/show-evidence.sh" "$START_TIME" "BLOCK — CREDIT_CARD blocked"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  VERDICT: ✓ Credit card detected — request blocked."
echo "  Check GATEWAY EVIDENCE — BlockedPiiEntityError should appear."
echo "  Anthropic never saw the request."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
