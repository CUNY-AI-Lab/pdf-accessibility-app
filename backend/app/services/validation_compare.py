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

    candidate_warnings = warning_count(candidate)
    current_warnings = warning_count(current)
    if candidate_warnings != current_warnings:
        return candidate_warnings < current_warnings

    return len(candidate.violations) < len(current.violations)
