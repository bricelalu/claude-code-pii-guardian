#!/usr/bin/env bash
# Scenario 2: AUDIT — soft PII is flagged in the logging_only layer but prompt is unchanged.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="sk-poc-local-only"

PROMPT='Our team meeting is scheduled for next Tuesday at our Paris office. Sarah will present the Q3 forecast.'

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  SCENARIO 2: AUDIT                                          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "▶ WHAT THE OPERATOR TYPED (soft PII — below blocking threshold):"
echo "  $PROMPT"
echo ""

START_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "▶ RUNNING AGAINST GATEWAY..."
echo ""

RESPONSE=$(claude -p "$PROMPT" 2>&1) || {
  echo "  ERROR from Claude Code: $RESPONSE"
  exit 1
}

echo "▶ CLAUDE'S RESPONSE (prompt reached Anthropic unmodified):"
echo "$RESPONSE" | sed 's/^/  /'
echo ""

"$SCRIPT_DIR/show-evidence.sh" "$START_TIME" "AUDIT — guardrail logging_only"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  VERDICT: ✓ Soft PII not blocked, but flagged for review."
echo "  Check GATEWAY EVIDENCE — presidio-audit should log detections"
echo "  for LOCATION=Paris, PERSON=Sarah, DATE_TIME=next Tuesday."
echo "  This is the safety net for a DPO audit trail."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
