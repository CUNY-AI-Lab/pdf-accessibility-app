#!/usr/bin/env bash
# Run pipeline_bench.py over the four old_scans parts in the Docling image,
# at most two containers at a time (Docling's memory use makes more unsafe),
# re-running any part until every page has output.
#   run_pipeline_bench.sh <candidate> [--worktree] [--gateway-ocr]
# --worktree     runs a snapshot of this worktree's app/ instead of the image's.
# --gateway-ocr  recognizes text through the CAIL Gateway (OCR_ENGINE=gateway),
#                with the key from the Keychain item "cail-gateway". The app's
#                other LLM calls name a model the Gateway rejects, so they fail
#                without inference, as they do in the other runs.
set -uo pipefail
candidate=$1; shift
backend=$(cd "$(dirname "$0")/.." && pwd)
bench=$backend/data/eval/olmocr-bench/bench_data
mounts=(-v "$bench:/bench"
  -v "$backend/scripts/pipeline_bench.py:/app/backend/pipeline_bench.py"
  -v "$backend/app/services/structure_text.py:/app/backend/app/services/structure_text.py")
env=(-e TORCHDYNAMO_DISABLE=1 -e LLM_BASE_URL=http://127.0.0.1:9/v1 -e LLM_MODEL=gemini-unused)
for flag in "$@"; do
  case $flag in
    --worktree)
      snapshot=$bench/.app-snapshot-$candidate
      rm -rf "$snapshot"; cp -R "$backend/app" "$snapshot"
      mounts=(-v "$bench:/bench" -v "$backend/scripts/pipeline_bench.py:/app/backend/pipeline_bench.py"
        -v "$snapshot:/app/backend/app") ;;
    --gateway-ocr)
      export LLM_API_KEY; LLM_API_KEY=$(security find-generic-password -s cail-gateway -w)
      env=(-e TORCHDYNAMO_DISABLE=1 -e OCR_ENGINE=gateway -e LLM_API_KEY
        -e LLM_BASE_URL=https://tools.ailab.gc.cuny.edu/v1 -e LLM_MODEL=gemini-unused) ;;
    *) echo "unknown flag $flag" >&2; exit 2 ;;
  esac
done
remaining() { # pages in part $1 without output
  local n=0
  for f in "$bench/pdfs/old_scans_part$1"/*.pdf; do
    [ -f "$bench/$candidate/old_scans_part$1/$(basename "$f" .pdf)_pg1_repeat1.md" ] || n=$((n+1))
  done
  echo $n
}
while :; do
  todo=()
  for p in 0 1 2 3; do [ "$(remaining $p)" -gt 0 ] && todo+=("$p"); done
  [ ${#todo[@]} -eq 0 ] && break
  for p in "${todo[@]}"; do
    name=bench-$candidate-$p
    docker ps --format "{{.Names}}" | grep -qxE "$name|pipe-part$p" && continue
    while [ "$(docker ps --format '{{.Names}}' | grep -cE '^(bench-|pipe-part)')" -ge 2 ]; do sleep 30; done
    docker run -d --rm --name "$name" --user 0 "${env[@]}" "${mounts[@]}" \
      pdf-a11y-eval:docling sh -c "python pipeline_bench.py --bench-dir /bench --candidate $candidate --subsets old_scans_part$p >> /bench/$candidate.part$p.log 2>&1" >/dev/null
    sleep 20
  done
  sleep 60
done
echo "all old_scans pages done for $candidate"
