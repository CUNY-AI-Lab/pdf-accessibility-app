"""Recompute release labels from saved run.json verdicts without opening PDFs.

Run from backend/ with PYTHONPATH=. and pass the directory containing the sets.
JSON goes to stdout; the source files are only read.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from app.services.release_policy import FIDELITY_HOLD_CHECKS, release_hold_reasons


def replay(root: Path) -> dict:
    if root.is_file():
        # The checked-in e128b6e fixture contains decisions/reasons, not raw
        # check verdicts. Replay that saved evidence; never fabricate PDFs.
        saved = json.loads(root.read_text())
        documents = []
        for row in saved["documents"]:
            reasons = release_hold_reasons(
                compliant="compliance" not in row["hold_reasons"],
                checks=[{"check": reason, "status": "fail" if reason in FIDELITY_HOLD_CHECKS else "error"}
                        for reason in row["hold_reasons"] if reason != "compliance"],
            )
            after = "manual_remediation" if reasons else "complete"
            if after != row["after"] or reasons != row["hold_reasons"]:
                raise ValueError(f"Release decision changed: {row['set']}/{row['document']}")
            documents.append({**row, "after": after, "hold_reasons": reasons})
    else:
        documents = _replay_runs(root)
    return {"summary": _summary(documents), "documents": documents}


def _replay_runs(root: Path) -> list[dict]:
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

    return documents


def _summary(documents: list[dict]) -> dict:
    summary = {}
    for name in sorted({doc["set"] for doc in documents}) + ["total"]:
        rows = [doc for doc in documents if name == "total" or doc["set"] == name]
        summary[name] = {
            "documents": len(rows),
            "before": dict(Counter(doc["before"] for doc in rows)),
            "after": dict(Counter(doc["after"] for doc in rows)),
            "hold_reasons": dict(Counter(reason for doc in rows for reason in doc["hold_reasons"])),
        }
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    options = parser.parse_args()
    print(json.dumps(replay(options.root), indent=2))
