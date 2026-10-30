#!/usr/bin/env bash
# run_a.sh <candidate> <mode> <subsets...>: the pipeline with the local-OCR plugin in <mode>.
set -u
cand=$1 mode=$2; shift 2
for s in "$@"; do
  ./queue.sh "$cand" "$cand" "$s" -- --shards "${SHARDS:-6}" \
    --local-ocr http://host.docker.internal:8131/v1 olmocr-2-7b-1025-mlx-8bit \
    --ocr-plugin /Users/stephenzweibel/Apps/cail/.worktrees/pdf-a11y-experiments/backend/data/exp/ocr_local_plugin.py \
    --env "LOCAL_OCR_MODE=$mode" --env "LOCAL_OCR_SIDECAR=/cache/ocr-sidecar/$cand" \
    --env OCR_GATEWAY_PAGE_SECONDS=1500 ${GUARD_ENV:+--env "$GUARD_ENV"} ${GUARD_ENV2:+--env "$GUARD_ENV2"}
done
