"""Ordered stages shared by the pipeline, runner, and benchmarks."""

PIPELINE_STEPS = ["classify", "ocr", "structure", "alt_text", "tagging", "validation", "fidelity"]
PIPELINE_STEP_ORDER = {name: idx for idx, name in enumerate(PIPELINE_STEPS)}
