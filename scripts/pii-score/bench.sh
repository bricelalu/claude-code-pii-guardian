#!/usr/bin/env bash
# Benchmarks Presidio analyzer variants (different NER backends) on PERSON and
# LOCATION detection in the pii-score CSV/Markdown/JSON exports and a generated
# corpus of real source files (Terraform, TS, Python, Go, Java, Rust) with PII in
# comments/fixtures, in FR/EN/ES/IT. Then masks each file with each model's
# detections and checks the file still parses/compiles.
# Each variant runs as a local Docker container, one at a time (memory), with
# model weights baked in and Hugging Face offline mode on. No gateway needed.
#
# Env: BENCH_LANGS (default fr,en,es,it), BENCH_VARIANTS (default all).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT_DIR="$REPO_ROOT/.pii-score-out/bench"
ANALYZERS_DIR="$REPO_ROOT/bench/analyzers"
BASE_IMAGE="mcr.microsoft.com/presidio-analyzer:latest@sha256:286e3fa7f3a7426e775e8564fe1870f1ba8f999d3ab8bbb8cc46a44355d9d6e9"
LANGS="${BENCH_LANGS:-fr,en,es,it}"
VARIANTS="${BENCH_VARIANTS:-spacy-en spacy-multi gliner gliner2}"
PORT=18090
CONTAINER=pii-bench-analyzer

for bin in docker sqlite3 jq python3 curl; do
  command -v "$bin" >/dev/null || { echo "ERROR: $bin is required." >&2; exit 1; }
done

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR/raw" "$OUT_DIR/exports"

echo "▶ Generating code corpus ($LANGS)..."
python3 "$REPO_ROOT/bench/corpus/generate.py" --out "$OUT_DIR/corpus" --langs "$LANGS" --check
bash "$SCRIPT_DIR/export.sh" "$OUT_DIR/exports"
python3 "$SCRIPT_DIR/span_score.py" add-exports --exports "$OUT_DIR/exports" --corpus "$OUT_DIR/corpus"

image_for() {
  case "$1" in
    spacy-en) echo "$BASE_IMAGE" ;;
    *)        echo "pii-bench/$1" ;;
  esac
}

for variant in $VARIANTS; do
  image="$(image_for "$variant")"
  if [ "$variant" != "spacy-en" ] && ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "▶ Building $image (first run downloads the model)..."
    docker build -t "$image" -f "$ANALYZERS_DIR/$variant/Dockerfile" "$ANALYZERS_DIR"
  fi

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  VARIANT: $variant"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  cleanup
  docker run -d --name "$CONTAINER" -p "127.0.0.1:${PORT}:3000" -e WORKERS=1 "$image" >/dev/null
  for _ in $(seq 1 180); do
    curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
    sleep 2
  done
  curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null || {
    echo "ERROR: $variant did not become healthy. Last logs:" >&2
    docker logs --tail 40 "$CONTAINER" >&2
    exit 1
  }

  extra=()
  [ "$variant" = "spacy-multi" ] && extra=(--per-file-language)
  python3 "$SCRIPT_DIR/span_score.py" collect --variant "$variant" \
    --analyzer "http://127.0.0.1:${PORT}" --corpus "$OUT_DIR/corpus" \
    --out "$OUT_DIR/raw/$variant.json" ${extra[@]+"${extra[@]}"}
done
cleanup

python3 "$SCRIPT_DIR/span_score.py" report "$OUT_DIR"/raw/*.json \
  --corpus "$OUT_DIR/corpus" --masked-dir "$OUT_DIR/masked" --out "$OUT_DIR/bench-results.json" | tee "$OUT_DIR/report.txt"

echo ""
echo "Results: $OUT_DIR/report.txt, $OUT_DIR/bench-results.json, raw detections in $OUT_DIR/raw/"
