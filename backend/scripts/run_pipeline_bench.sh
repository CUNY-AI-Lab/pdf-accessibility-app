#!/usr/bin/env bash
# Run pipeline_bench.py over one benchmark subset in parallel containers, with
# structure from a docling-serve on the host (DOCLING_SERVE_URL, as in
# production).
#   run_pipeline_bench.sh <candidate> <subset> [--worktree] [--gateway-ocr] [--shards N]
# By default the pipeline is production's, from pdf-a11y-eval:prod.
# --worktree     runs this worktree's app/ (a snapshot, so later edits do not
#                leak into a running bench) in pdf-a11y-eval:branch, built
#                from this worktree: docker build -t pdf-a11y-eval:branch .
# --gateway-ocr  recognizes text through the CAIL Gateway (OCR_ENGINE=gateway),
#                with the key from the Keychain item "cail-gateway".
# The app's own LLM calls fail at once in every run (no LLM is reachable and
# the direct Gemini path is off), so runs compare the deterministic pipeline.
set -euo pipefail
candidate=$1 subset=$2
shift 2
backend=$(cd "$(dirname "$0")/.." && pwd)
bench=$backend/data/eval/olmocr-bench/bench_data
shards=4
image=pdf-a11y-eval:prod
snapshot=
llm_base_url=http://127.0.0.1:9/v1
env=(-e DOCLING_SERVE_URL=http://host.docker.internal:5001 -e LLM_MAX_RETRIES=0
  -e USE_DIRECT_GEMINI_PDF=false -e LLM_MODEL=gemini-unused)
mounts=(-v "$bench:/bench" -v "$backend/scripts/pipeline_bench.py:/app/backend/pipeline_bench.py:ro")
while [ $# -gt 0 ]; do
  case $1 in
    --worktree)
      image=pdf-a11y-eval:branch
      snapshot=$(mktemp -d "$bench/.app-snapshot-XXXXXX")
      cp -R "$backend/app/." "$snapshot"
      mounts+=(-v "$snapshot:/app/backend/app:ro") ;;
    --gateway-ocr)
      LLM_API_KEY=$(security find-generic-password -s cail-gateway -w)
      export LLM_API_KEY
      llm_base_url=https://tools.ailab.gc.cuny.edu/v1
      env+=(-e OCR_ENGINE=gateway -e LLM_API_KEY) ;;
    --shards) shards=$2; shift ;;
    *) echo "unknown flag $1" >&2; exit 2 ;;
  esac
  shift
done
env+=(-e "LLM_BASE_URL=$llm_base_url")
mkdir -p "$bench/logs"
[ -n "$snapshot" ] && trap 'rm -rf "$snapshot"' EXIT

for i in $(seq 0 $((shards - 1))); do
  docker run --rm --name "pipe-$candidate-$subset-$i" --user 0 "${env[@]}" "${mounts[@]}" \
    "$image" sh -c "cd /app/backend && python pipeline_bench.py --bench-dir /bench \
      --candidate $candidate --subsets $subset --shard $i/$shards" \
    >> "$bench/logs/$candidate.$subset.$i.log" 2>&1 &
done
wait
out=$bench/$candidate/$subset
echo "$candidate $subset: $(find "$out" -name '*.tagged.pdf' | wc -l) tagged," \
  "$(find "$out" -name '*.failed' | wc -l) failed," \
  "of $(find "$bench/pdfs/$subset" -name '*.pdf' | wc -l) PDFs"
