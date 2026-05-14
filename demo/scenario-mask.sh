#!/usr/bin/env bash
# Scenario 1: MASK — email, name, and phone number are redacted before reaching Anthropic.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="sk-poc-local-only"

PROMPT='Please rewrite this email more politely: '\''Hey John Smith, your delivery to john.smith@example.com is delayed. Call us at +33 6 12 34 56 78.'\'''

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  SCENARIO 1: MASK                                           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "▶ WHAT THE OPERATOR TYPED (PII visible):"
echo "  $PROMPT"
echo ""

START_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "▶ RUNNING AGAINST GATEWAY..."
echo ""

RESPONSE=$(claude -p "$PROMPT" 2>&1) || {
  echo "  ERROR from Claude Code: $RESPONSE"
  exit 1
}

echo "▶ CLAUDE'S RESPONSE:"
echo "$RESPONSE" | sed 's/^/  /'
echo ""

"$SCRIPT_DIR/show-evidence.sh" "$START_TIME" "MASK — guardrail pre_call"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  VERDICT: ✓ PII redacted before reaching Anthropic."
echo "  Check GATEWAY EVIDENCE above — [PERSON], [EMAIL_ADDRESS],"
echo "  [PHONE_NUMBER] should appear in the forwarded request body."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
