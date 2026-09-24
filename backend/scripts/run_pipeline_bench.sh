#!/usr/bin/env bash
# Run pipeline_bench.py over one olmOCR-Bench subset in the prod image, with
# structure from a docling-serve on the host (DOCLING_SERVE_URL, as in
# production), split across parallel containers.
#   run_pipeline_bench.sh <candidate> <subset> [--worktree] [--gateway-ocr] [--shards N]
# --worktree     runs a snapshot of this worktree's app/ instead of the image's.
# --gateway-ocr  recognizes text through the CAIL Gateway (OCR_ENGINE=gateway),
#                with the key from the Keychain item "cail-gateway".
# The app's own LLM calls fail at once in every run (no retries), so runs
# compare the deterministic pipeline.
set -uo pipefail
candidate=$1 subset=$2; shift 2
backend=$(cd "$(dirname "$0")/.." && pwd)
bench=$backend/data/eval/olmocr-bench/bench_data
shards=4
mounts=(-v "$bench:/bench"
  -v "$backend/scripts/pipeline_bench.py:/app/backend/pipeline_bench.py:ro"
  -v "$backend/app/services/structure_text.py:/app/backend/app/services/structure_text.py:ro")
env=(-e DOCLING_SERVE_URL=http://host.docker.internal:5001 -e LLM_MAX_RETRIES=0 -e USE_DIRECT_GEMINI_PDF=false
  -e LLM_BASE_URL=http://127.0.0.1:9/v1 -e LLM_MODEL=gemini-unused)
while [ $# -gt 0 ]; do
  case $1 in
    --worktree)
      snapshot=$bench/.app-snapshot-$candidate-$subset
      rm -rf "$snapshot"; cp -R "$backend/app" "$snapshot"
      mounts=(-v "$bench:/bench" -v "$backend/scripts/pipeline_bench.py:/app/backend/pipeline_bench.py:ro"
        -v "$snapshot:/app/backend/app:ro") ;;
    --gateway-ocr)
      export LLM_API_KEY; LLM_API_KEY=$(security find-generic-password -s cail-gateway -w)
      env=(-e DOCLING_SERVE_URL=http://host.docker.internal:5001 -e LLM_MAX_RETRIES=0 -e USE_DIRECT_GEMINI_PDF=false
        -e OCR_ENGINE=gateway -e LLM_API_KEY
        -e LLM_BASE_URL=https://tools.ailab.gc.cuny.edu/v1 -e LLM_MODEL=gemini-unused) ;;
    --shards) shards=$2; shift ;;
    *) echo "unknown flag $1" >&2; exit 2 ;;
  esac
  shift
done
for i in $(seq 0 $((shards - 1))); do
  docker run --rm --name "pipe-$candidate-$subset-$i" --user 0 "${env[@]}" "${mounts[@]}" \
    pdf-a11y-eval:prod sh -c "cd /app/backend && python pipeline_bench.py --bench-dir /bench \
      --candidate $candidate --subsets $subset --shard $i/$shards" \
    >> "$bench/$candidate.$subset.$i.log" 2>&1 &
done
wait
echo "$candidate $subset: $(ls "$bench/$candidate/$subset"/*.md 2>/dev/null | wc -l) of $(ls "$bench/pdfs/$subset"/*.pdf | wc -l) pages"
