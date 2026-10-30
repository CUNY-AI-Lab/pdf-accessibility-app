#!/usr/bin/env bash
# queue.sh <log name> <candidate> <subset>... -- <run_pipeline_bench flags>: run subsets in turn.
set -u
name=$1 cand=$2; shift 2
subsets=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do subsets+=("$1"); shift; done
[ $# -gt 0 ] && shift
backend=/Users/stephenzweibel/Apps/cail/.worktrees/pdf-a11y-experiments/backend
log=$backend/data/exp/logs/queue-$name.log
for subset in "${subsets[@]}"; do
  start=$(date +%s)
  echo "$(date '+%F %T') start $cand $subset" >> "$log"
  bash "$backend/scripts/run_pipeline_bench.sh" "$cand" "$subset" --image pdf-a11y-eval:8a81fdc \
    --docling-url http://host.docker.internal:5011 --docling-device mps "$@" >> "$log" 2>&1
  status=$?
  echo "$(date '+%F %T') done $cand $subset exit=$status seconds=$(( $(date +%s) - start ))" >> "$log"
done
echo "$(date '+%F %T') queue finished" >> "$log"
