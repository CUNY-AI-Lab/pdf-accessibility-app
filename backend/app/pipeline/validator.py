"""Step 6: Validate PDF/UA compliance using veraPDF."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.pipeline.subprocess_utils import SubprocessTimeout, communicate_with_timeout
from app.services.runtime_paths import enriched_subprocess_env, resolve_binary

logger = logging.getLogger(__name__)


@dataclass
class Violation:
    rule_id: str
    description: str
    severity: str  # "error" or "warning"
    location: str | None = None
    count: int = 1
    category: str | None = None
    fix_hint: str | None = None


@dataclass
class ValidationResult:
    compliant: bool
    violations: list[Violation] = field(default_factory=list)
    raw_report: dict = field(default_factory=dict)
    error: str | None = None


RULE_GUIDANCE: dict[str, dict[str, str]] = {
    "7.4.2-1": {
        "category": "headings",
        "fix_hint": "Ensure the first heading is H1 and heading levels do not skip.",
    },
    "7.5-1": {
        "category": "tables",
        "fix_hint": "Set TH scope (Row/Column) or provide explicit Headers/IDs relationships.",
    },
    "7.18.1-1": {
        "category": "annotations",
        "fix_hint": "Add /Contents or equivalent alternate text for non-widget annotations.",
    },
    "7.18.1-2": {
        "category": "annotations",
        "fix_hint": "Add /Contents or alternate text for visible annotations inside crop box.",
    },
    "7.18.3-1": {
        "category": "annotations",
        "fix_hint": "Set page /Tabs to /S when annotations are present.",
    },
    "7.18.4-1": {
        "category": "forms",
        "fix_hint": "Nest widget annotations under /Form structure elements via OBJR.",
    },
    "7.18.5-1": {
        "category": "links",
        "fix_hint": "Tag link annotations as /Link elements and connect via ParentTree.",
    },
    "7.18.5-2": {
        "category": "links",
        "fix_hint": "Provide descriptive /Contents for link annotations.",
    },
    "5-1": {
        "category": "metadata",
        "fix_hint": "Set PDF/UA identification metadata (pdfuaid:part) in valid XMP.",
    },
    "7.21.4.1-1": {
        "category": "fonts",
        "fix_hint": "Embed all fonts used by the document.",
    },
    "7.21.4.2-2": {
        "category": "fonts",
        "fix_hint": "Ensure CIDSet covers all CIDs present in embedded CID fonts.",
    },
    "7.21.7-1": {
        "category": "fonts",
        "fix_hint": "Ensure fonts map used character codes to Unicode via ToUnicode or equivalent.",
    },
    "7.21.7-2": {
        "category": "fonts",
        "fix_hint": "Ensure every ToUnicode CMap mapping resolves to a valid Unicode value (not 0, U+FEFF, or U+FFFE).",
    },
    "7.21.8-1": {
        "category": "fonts",
        "fix_hint": "Eliminate .notdef glyph references in text-showing operators by fixing font subsets/encoding.",
    },
    "7.21.3.2-1": {
        "category": "fonts",
        "fix_hint": "Provide CIDToGIDMap for Type 2 CIDFonts (or Identity where applicable).",
    },
}


def _rule_key(clause: str, test_number: str) -> str:
    clause_str = str(clause or "").strip()
    test_str = str(test_number or "").strip()
    if not clause_str or not test_str:
        return ""
    return f"{clause_str}-{test_str}"


def _guidance_for(clause: str, test_number: str) -> tuple[str | None, str | None]:
    key = _rule_key(clause, test_number)
    meta = RULE_GUIDANCE.get(key)
    if not meta:
        return None, None
    return meta.get("category"), meta.get("fix_hint")


async def validate_pdf(
    pdf_path: Path,
    verapdf_path: str = "verapdf",
    flavour: str = "ua1",
    timeout_seconds: int | None = None,
) -> ValidationResult:
    """Run veraPDF PDF/UA validation and parse results."""
    try:
        return await _validate_with_verapdf(
            pdf_path, verapdf_path, flavour, timeout_seconds
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"veraPDF executable not found at '{verapdf_path}'. "
            "Install veraPDF or set VERAPDF_PATH."
        ) from exc


async def _validate_with_verapdf(
    pdf_path: Path,
    verapdf_path: str,
    flavour: str,
    timeout_seconds: int | None = None,
) -> ValidationResult:
    """Full validation via veraPDF CLI."""
    resolved_verapdf = resolve_binary("verapdf", explicit=verapdf_path)
    if not resolved_verapdf:
        raise FileNotFoundError(verapdf_path)
    proc = await asyncio.create_subprocess_exec(
        resolved_verapdf,
        "-f",
        flavour,
        "--format",
        "json",
        "--maxfailuresdisplayed",
        "50",
        str(pdf_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=enriched_subprocess_env(),
    )
    try:
        stdout, stderr = await communicate_with_timeout(proc, timeout_seconds)
    except SubprocessTimeout:
        return ValidationResult(
            compliant=False,
            error=f"veraPDF timed out after {timeout_seconds}s",
        )

    if proc.returncode not in (0, 1):
        # 0 = compliant, 1 = violations found, other = error
        error_text = stderr.decode("utf-8", errors="replace")
        return ValidationResult(
            compliant=False,
            error=f"veraPDF error (exit {proc.returncode}): {error_text}",
        )

    try:
        report = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as e:
        return ValidationResult(
            compliant=False,
            error=f"Failed to parse veraPDF output: {e}",
        )

    # Parse violations from veraPDF JSON report
    violations = []
    compliant = True

    # veraPDF JSON structure varies by version; handle common formats
    report_root = report.get("report", {}) if isinstance(report, dict) else {}
    jobs = report_root.get("jobs", []) if isinstance(report_root, dict) else []
    if jobs:
        job = jobs[0]
        validation_raw = job.get("validationResult", {})
        validation_entries = (
            validation_raw
            if isinstance(validation_raw, list)
            else [validation_raw]
        )
        validation_entries = [entry for entry in validation_entries if isinstance(entry, dict)]

        if validation_entries:
            compliant = all(bool(entry.get("compliant", False)) for entry in validation_entries)
        else:
            compliant = False

        for validation in validation_entries:
            details = validation.get("details", {})
            if not isinstance(details, dict):
                continue

            # Older veraPDF JSON schema.
            legacy_rules = details.get("rule", [])
            if isinstance(legacy_rules, list):
                for detail in legacy_rules:
                    if not isinstance(detail, dict):
                        continue
                    clause = str(detail.get("clause", ""))
                    test_number = str(detail.get("testNumber", ""))
                    category, fix_hint = _guidance_for(clause, test_number)
                    rule_id = (
                        f"{detail.get('specification', '')}-"
                        f"{clause}-"
                        f"{test_number}"
                    )
                    violations.append(Violation(
                        rule_id=rule_id,
                        description=detail.get("description", "Unknown violation"),
                        severity="error",
                        count=detail.get("failedChecks", 1),
                        category=category,
                        fix_hint=fix_hint,
                    ))

            # Newer veraPDF JSON schema.
            rule_summaries = details.get("ruleSummaries", [])
            if not isinstance(rule_summaries, list):
                continue
            for summary in rule_summaries:
                if not isinstance(summary, dict):
                    continue
                status = str(summary.get("status", "")).lower()
                if status == "passed":
                    continue
                failed_checks = summary.get("failedChecks", 1)
                clause = str(summary.get("clause", ""))
                test_number = str(summary.get("testNumber", ""))
                category, fix_hint = _guidance_for(clause, test_number)
                rule_id = (
                    f"{summary.get('specification', '')}-"
                    f"{clause}-"
                    f"{test_number}"
                )
                violations.append(Violation(
                    rule_id=rule_id,
                    description=summary.get("description", "Unknown violation"),
                    severity="error" if status in ("failed", "error") else "warning",
                    location=summary.get("object"),
                    count=failed_checks if isinstance(failed_checks, int) else 1,
                    category=category,
                    fix_hint=fix_hint,
                ))

    return ValidationResult(
        compliant=compliant,
        violations=violations,
        raw_report=report,
    )
