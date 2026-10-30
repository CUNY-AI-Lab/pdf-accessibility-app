#!/usr/bin/env bash
# Run pipeline_bench.py over one benchmark subset in parallel containers, with
# structure from a docling-serve on the host (DOCLING_SERVE_URL, as in
# production).
#   run_pipeline_bench.sh <candidate> <subset> [--image <name>] [--worktree]
#     [--gateway-ocr] [--llm <model>] [--cassette-mode <mode>] [--shards N]
#     [--docling-device <device>] [--no-docling-cache]
# The pipeline runs in pdf-a11y-eval:branch, the production image's recipe
# built from this checkout:
#   docker build --label org.opencontainers.image.revision=$(git rev-parse HEAD) \
#     -t pdf-a11y-eval:branch .
# --image        runs another image instead, such as a published runner image
#                (ghcr.io/cuny-ai-lab/pdf-accessibility-app:<commit>).
# --worktree     runs this worktree's app/ (a snapshot, so later edits do not
#                leak into a running bench) in that image.
# --gateway-ocr  recognizes text through the CAIL Gateway (OCR_ENGINE=gateway),
#                with the key from the Keychain item "cail-gateway".
# --llm          turns the app's AI steps on with that Gateway model; model
#                calls go through a cassette in data/eval/cache/llm-cassette,
#                replayed where it has them (--cassette-mode record|replay|strict).
# --docling-device  the device docling-serve runs on, for capture.json.
# --docling-url  the docling-serve the containers reach (default
#                http://host.docker.internal:5001).
# --docling-option key=value  adds a docling-serve request option to the
#                app's own (e.g. table_cell_matching=false); repeatable.
# --local-ocr <base url> <model>  recognizes text with a model on a local
#                OpenAI-compatible server (OCR_ENGINE=gateway, no key); the AI
#                steps get a model name the server refuses.
# --ocr-plugin <file>  replaces app/pipeline/ocr_vlm_plugin.py with <file>.
# --env KEY=VALUE  passes an environment variable to the containers.
# Without --llm no model is reachable and the AI steps fail at once, so runs
# compare the deterministic pipeline. Conversions are cached by PDF bytes,
# request options and Docling version in data/eval/cache/docling-serve.
set -euo pipefail
candidate=$1 subset=$2
shift 2
backend=$(cd "$(dirname "$0")/.." && pwd)
bench=$backend/data/eval/olmocr-bench/bench_data
cache=$backend/data/eval/cache
shards=4
image=pdf-a11y-eval:branch
app_source=image
snapshot=
gateway=https://tools.ailab.gc.cuny.edu/v1
llm=
gateway_ocr=
cassette_mode=replay
docling_device=unknown
docling_cache=1
docling_url=http://host.docker.internal:5001
docling_options=()
local_ocr=
local_ocr_model=
# A long document takes docling-serve on a Mac GPU longer than production's
# five minutes.
env=(-e DOCLING_SERVE_TIMEOUT=1800)
mounts=(-v "$bench:/bench" -v "$cache:/cache" -v "$backend/scripts:/app/backend/scripts:ro")
while [ $# -gt 0 ]; do
  case $1 in
    --image) image=$2; shift ;;
    --worktree)
      app_source=worktree
      snapshot=$(mktemp -d "$bench/.app-snapshot-XXXXXX")
      cp -R "$backend/app/." "$snapshot"
      mounts+=(-v "$snapshot:/app/backend/app:ro") ;;
    --gateway-ocr) gateway_ocr=1 ;;
    --llm) llm=$2; shift ;;
    --cassette-mode) cassette_mode=$2; shift ;;
    --shards) shards=$2; shift ;;
    --docling-device) docling_device=$2; shift ;;
    --no-docling-cache) docling_cache= ;;
    --docling-url) docling_url=$2; shift ;;
    --docling-option) docling_options+=("$2"); shift ;;
    --local-ocr) local_ocr=$2 local_ocr_model=$3; shift 2 ;;
    --ocr-plugin)
      mounts+=(-v "$(cd "$(dirname "$2")" && pwd)/$(basename "$2"):/app/backend/app/pipeline/ocr_vlm_plugin.py:ro")
      env+=(-e "BENCH_OCR_PLUGIN=$(basename "$2")"); shift ;;
    --env) env+=(-e "$2"); shift ;;
    *) echo "unknown flag $1" >&2; exit 2 ;;
  esac
  shift
done
env+=(-e "DOCLING_SERVE_URL=$docling_url")
if [ ${#docling_options[@]} -gt 0 ]; then
  json=$(python3 -c 'import json,sys; print(json.dumps(dict(a.split("=",1) for a in sys.argv[1:])))' "${docling_options[@]}")
  env+=(-e "BENCH_DOCLING_OPTIONS=$json")
fi
# The Gateway key goes to the containers only when they call the Gateway.
if [ -n "$gateway_ocr" ] || [ -n "$llm" ]; then
  LLM_API_KEY=$(security find-generic-password -s cail-gateway -w)
  export LLM_API_KEY
  env+=(-e LLM_API_KEY)
fi
[ -n "$gateway_ocr" ] && env+=(-e OCR_ENGINE=gateway)
bench_args=(--bench-dir /bench --candidate "$candidate" --subsets "$subset")
[ -n "$docling_cache" ] && bench_args+=(--docling-cache /cache/docling-serve)
if [ -n "$local_ocr" ]; then
  # The AI steps reach the local server with a model name it refuses, so
  # they fail at once, as without --llm.
  env+=(-e OCR_ENGINE=gateway -e "OCR_MODEL=$local_ocr_model" -e "LLM_BASE_URL=$local_ocr"
    -e LLM_API_KEY=local -e LLM_MODEL=ai-steps-off -e LLM_MAX_RETRIES=0 -e BENCH_AI_STEPS=off)
elif [ -n "$llm" ]; then
  env+=(-e "LLM_BASE_URL=$gateway" -e "LLM_MODEL=$llm" -e BENCH_AI_STEPS="$llm")
  bench_args+=(--cassette /cache/llm-cassette --cassette-mode "$cassette_mode")
else
  # With Gateway OCR the AI steps reach the Gateway but name no model it
  # serves, so they fail at once; otherwise nothing listens at the address.
  base_url=http://127.0.0.1:9/v1
  [ -n "$gateway_ocr" ] && base_url=$gateway
  env+=(-e "LLM_BASE_URL=$base_url" -e LLM_MODEL=ai-steps-off -e LLM_MAX_RETRIES=0
    -e BENCH_AI_STEPS=off)
fi
# The commit of the app code that runs: the image's, or this checkout's.
revision='{{index .Config.Labels "org.opencontainers.image.revision"}}'
commit=$(docker image inspect -f "$revision" "$image")
if [ "$app_source" = worktree ]; then
  commit=$(git -C "$backend" rev-parse HEAD)
  if [ -n "$(git -C "$backend" status --porcelain -- app)" ]; then commit=$commit-dirty; fi
fi
env+=(-e "BENCH_GIT_COMMIT=${commit:-unknown}" -e "BENCH_APP_SOURCE=$app_source"
  -e "BENCH_IMAGE=$image" -e "BENCH_IMAGE_ID=$(docker image inspect -f '{{.Id}}' "$image")"
  -e "BENCH_DOCLING_DEVICE=$docling_device")
mkdir -p "$bench/logs" "$cache"
[ -n "$snapshot" ] && trap 'rm -rf "$snapshot"' EXIT

for i in $(seq 0 $((shards - 1))); do
  docker run --rm --name "pipe-$candidate-$subset-$i" --user 0 "${env[@]}" "${mounts[@]}" \
    -e PYTHONPATH=/app/backend "$image" \
    python /app/backend/scripts/pipeline_bench.py "${bench_args[@]}" --shard "$i/$shards" \
    >> "$bench/logs/$candidate.$subset.$i.log" 2>&1 &
done
wait
out=$bench/$candidate/$subset
echo "$candidate $subset: $(find "$out" -name '*.tagged.pdf' | wc -l) tagged," \
  "$(find "$out" -name '*.failed' | wc -l) failed," \
  "of $(find "$bench/pdfs/$subset" -name '*.pdf' | wc -l) PDFs"
