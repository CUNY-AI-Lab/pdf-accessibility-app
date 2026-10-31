"""Recompute release labels from saved run.json verdicts without opening PDFs.

Run from backend/ with PYTHONPATH=. and pass the directory containing the sets.
JSON goes to stdout; the source files are only read.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from app.services.release_policy import release_hold_reasons


def replay(root: Path) -> dict:
    documents = []
    for path in sorted(root.glob("*/*.run.json")):
        run = json.loads(path.read_text())
        outcome = run["outcome"]
        if not isinstance(outcome["validation_compliant"], bool):
            raise ValueError(f"Missing veraPDF verdict: {path}")
        before = run["status"].split()[0]
        if before not in {"complete", "manual_remediation"}:
            raise ValueError(f"No terminal release verdict: {path}")
        reasons = release_hold_reasons(
            compliant=outcome["validation_compliant"], checks=outcome["checks"],
        )
        documents.append({
            "set": path.parent.name,
            "document": path.name.removesuffix(".run.json"),
            "before": before,
            "after": "manual_remediation" if reasons else "complete",
            "hold_reasons": reasons,
        })
    if not documents:
        raise ValueError(f"No run.json files found: {root}")

    summary = {}
    for name in sorted({doc["set"] for doc in documents}) + ["total"]:
        rows = [doc for doc in documents if name == "total" or doc["set"] == name]
        summary[name] = {
            "documents": len(rows),
            "before": dict(Counter(doc["before"] for doc in rows)),
            "after": dict(Counter(doc["after"] for doc in rows)),
            "hold_reasons": dict(Counter(reason for doc in rows for reason in doc["hold_reasons"])),
        }
    return {"summary": summary, "documents": documents}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    options = parser.parse_args()
    print(json.dumps(replay(options.root), indent=2))
