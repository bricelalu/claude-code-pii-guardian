#!/usr/bin/env bash
# Create the RunPod endpoint described in endpoint.yaml (billable while workers run;
# $0 when idle). Prints the endpoint id and its load-balancer URL.
# Needs RUNPOD_API_KEY (env or .env) and yq v4.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ -z "${RUNPOD_API_KEY:-}" ] && [ -f "$REPO_ROOT/.env" ]; then
  RUNPOD_API_KEY="$(grep -E '^RUNPOD_API_KEY=' "$REPO_ROOT/.env" | cut -d= -f2- || true)"
fi
: "${RUNPOD_API_KEY:?RUNPOD_API_KEY is not set (env or .env)}"
command -v yq >/dev/null || { echo "ERROR: yq v4 is required." >&2; exit 1; }

yq -o=json "$SCRIPT_DIR/endpoint.yaml" \
  | curl -sS --fail-with-body -X POST https://api.runpod.io/v2/serverless \
      -H "Authorization: Bearer $RUNPOD_API_KEY" \
      -H "Content-Type: application/json" \
      --data-binary @- \
  | jq '{id, name, type, requestUrls}'
