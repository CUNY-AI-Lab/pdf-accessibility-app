#!/usr/bin/env bash
# score.sh <subset> <candidate>... [--against <base>]: olmOCR-Bench scores (tables as HTML), per-test results kept.
set -u
cd /Users/stephenzweibel/Apps/cail/.worktrees/pdf-a11y-experiments/backend
s=$1; shift
md=; case $s in tables*) md=--markdown ;; esac
PYTHONPATH=. UV_NO_CONFIG=1 ~/.local/bin/uvx uv@0.13.4 run --frozen --with "olmocr[bench]==0.4.27" \
  python scripts/score_olmocr.py --bench data/eval/olmocr-bench $md "$s" "$@" 2>&1 | grep -v "Loading tests\|it/s\|Cannot set non-stroke"
