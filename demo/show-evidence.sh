#!/usr/bin/env bash
# Called by scenario scripts to surface guardrail evidence from LiteLLM logs.
# Usage: show-evidence.sh <since-time-RFC3339> <scenario-label>
set -euo pipefail

SINCE="${1:-}"
LABEL="${2:-evidence}"

if [ -z "$SINCE" ]; then
  echo "Usage: show-evidence.sh <RFC3339-start-time> <label>" >&2
  exit 1
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  GATEWAY EVIDENCE — ${LABEL}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

RAW=$(kubectl logs -n gateway -l app=litellm --since-time="$SINCE" 2>/dev/null || true)

if [ -z "$RAW" ]; then
  echo "  (no log output captured — check: kubectl logs -n gateway -l app=litellm)"
  echo ""
  exit 0
fi

RELEVANT=$(echo "$RAW" | grep -iE 'guardrail|presidio|BlockedPii|blocked|masked|MASK|BLOCK|X-Claude-Code-Session-Id|session_id|messages' || true)

if [ -z "$RELEVANT" ]; then
  echo "  (no guardrail-related lines found in this window)"
  echo "  Raw log tail (last 10 lines):"
  echo "$RAW" | tail -10 | sed 's/^/    /'
  echo ""
  exit 0
fi

if command -v jq &>/dev/null; then
  echo "$RELEVANT" | while IFS= read -r line; do
    if echo "$line" | jq -e . &>/dev/null 2>&1; then
      echo "$line" | jq -C '
        if .messages then
          "REQUEST BODY → messages: " + (.messages | tostring)
        elif .message then
          .
        else
          .
        end
      ' 2>/dev/null || echo "  $line"
    else
      echo "  $line"
    fi
  done
else
  echo "$RELEVANT" | sed 's/^/  /'
fi

echo ""
