"""Shared helpers for comparing validation outcomes."""

from __future__ import annotations


def error_count(report) -> int:
    return sum(1 for violation in report.violations if violation.severity == "error")


def warning_count(report) -> int:
    return sum(1 for violation in report.violations if violation.severity != "error")


def is_better_validation(candidate, current) -> bool:
    """Return whether the candidate validation result is better than current."""
    if candidate.compliant and not current.compliant:
        return True

    candidate_errors = error_count(candidate)
    current_errors = error_count(current)
    if candidate_errors != current_errors:
        return candidate_errors < current_errors

    # A partial font repair can remove many failed checks while the rule
    # still fails on another font. Preserve that improvement, provided no
    # rule gains failures (an aggregate total could hide a regression).
    def counts(report):
        result = {}
        for violation in report.violations:
            if violation.severity == "error":
                key = violation.rule_id
                count = getattr(violation, "count", 1)
                weight = count if isinstance(count, int) and count > 0 else 1
                result[key] = result.get(key, 0) + weight
        return result

    candidate_counts, current_counts = counts(candidate), counts(current)
    if all(count <= current_counts.get(rule, 0) for rule, count in candidate_counts.items()):
        if sum(candidate_counts.values()) < sum(current_counts.values()):
            return True

    candidate_warnings = warning_count(candidate)
    current_warnings = warning_count(current)
    if candidate_warnings != current_warnings:
        return candidate_warnings < current_warnings

    return len(candidate.violations) < len(current.violations)
