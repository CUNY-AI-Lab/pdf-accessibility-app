#!/usr/bin/env bash
# run_app.sh <candidate> <subsets...>: the app's own Gateway OCR plugin, unchanged, pointed at the local server.
set -u
cand=$1; shift
for s in "$@"; do
  ./queue.sh "$cand" "$cand" "$s" -- --shards "${SHARDS:-4}" \
    --local-ocr http://host.docker.internal:8131/v1 olmocr-2-7b-1025-mlx-8bit \
    --env OCR_GATEWAY_PAGE_SECONDS=1500
done
