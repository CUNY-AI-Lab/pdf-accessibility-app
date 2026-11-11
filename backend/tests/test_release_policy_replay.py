import json

from scripts.replay_release_policy import replay


def test_replay_uses_named_verdicts_instead_of_old_blocking_flags(tmp_path):
    subset = tmp_path / "gold_rt"
    subset.mkdir()
    files = {}
    for name, check, verdict, compliant in [
        ("figure_warning", "figure_coverage", "warning", True),
        ("order_failure", "reading_order", "fail", True),
        ("coverage_warning", "tagged_text_coverage", "warning", True),
        ("text_loss", "tagged_text_coverage", "fail", True),
        ("no_headers", "table_headers", "fail", True),
        ("no_alt", "figure_descriptions", "fail", True),
        ("check_error", "link_text_quality", "error", True),
        ("noncompliant", "compliance", "fail", False),
    ]:
        path = subset / f"{name}.run.json"
        text = json.dumps({
            "status": "manual_remediation digital",
            "outcome": {
                "validation_compliant": compliant,
                "checks": [{"check": check, "status": verdict}],
                "review_tasks": [{"task_type": "figure_semantics", "blocking": True}],
            },
        })
        path.write_text(text)
        files[path] = text

    result = replay(tmp_path)

    assert result["summary"]["total"]["after"] == {"manual_remediation": 5, "complete": 3}
    released = {row["document"] for row in result["documents"] if row["after"] == "complete"}
    assert released == {"figure_warning", "order_failure", "coverage_warning"}
    assert all(path.read_text() == text for path, text in files.items())
