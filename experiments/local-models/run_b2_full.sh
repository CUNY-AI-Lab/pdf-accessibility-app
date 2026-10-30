#!/usr/bin/env bash
# PaddleOCR-VL on every Docling table of the whole tables categories; retag baseline; model-HTML upper bound.
set -u
cd /Users/stephenzweibel/Apps/cail/.worktrees/pdf-a11y-experiments/backend
for s in tables_scan tables; do
  data/exp/mlx/.venv/bin/python data/exp/table_vlm.py --bench data/eval/olmocr-bench/bench_data --source pipe_8a81fdc --subset $s --model paddle --out paddle_m80_inputs --min-match 0.8 2>&1 | tail -1
  PYTHONPATH=data/exp/app-8a81fdc/backend .venv/bin/python scripts/retag_bench.py --bench-dir data/eval/olmocr-bench/bench_data --source pipe_8a81fdc --candidate retag_base $s 2>&1 | tail -1
  data/exp/score.sh $s retag_base --against pipe_8a81fdc 2>&1 | grep -v "^    "
  data/exp/mlx/.venv/bin/python data/exp/table_direct.py $s paddle retag_base b2_paddle_direct | tail -1
  data/exp/score.sh $s b2_paddle_direct --against retag_base 2>&1 | grep -v "^    "
done
echo B2-FULL-DONE
