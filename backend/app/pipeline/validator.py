"""Step 6: Validate PDF/UA compliance using veraPDF."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Violation:
    rule_id: str
    description: str
    severity: str  # "error" or "warning"
    location: str | None = None
    count: int = 1


@dataclass
class ValidationResult:
    compliant: bool
    violations: list[Violation] = field(default_factory=list)
    raw_report: dict = field(default_factory=dict)
    error: str | None = None


async def validate_pdf(
    pdf_path: Path,
    verapdf_path: str = "verapdf",
    flavour: str = "ua1",
) -> ValidationResult:
    """Run veraPDF PDF/UA validation and parse results.

    Falls back to a basic check if veraPDF is not installed.
    """
    try:
        return await _validate_with_verapdf(pdf_path, verapdf_path, flavour)
    except FileNotFoundError:
        logger.warning("veraPDF not found, using basic validation")
        return await _validate_basic(pdf_path)


async def _validate_with_verapdf(
    pdf_path: Path,
    verapdf_path: str,
    flavour: str,
) -> ValidationResult:
    """Full validation via veraPDF CLI."""
    proc = await asyncio.create_subprocess_exec(
        verapdf_path,
        "-f",
        flavour,
        "--format",
        "json",
        "--maxfailuresdisplayed",
        "50",
        str(pdf_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

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
    jobs = report.get("report", {}).get("jobs", [])
    if jobs:
        job = jobs[0]
        validation = job.get("validationResult", {})
        compliant = validation.get("compliant", False)

        for detail in validation.get("details", {}).get("rule", []):
            rule_id = f"{detail.get('specification', '')}-{detail.get('clause', '')}-{detail.get('testNumber', '')}"
            violations.append(Violation(
                rule_id=rule_id,
                description=detail.get("description", "Unknown violation"),
                severity="error",
                count=detail.get("failedChecks", 1),
            ))

    return ValidationResult(
        compliant=compliant,
        violations=violations,
        raw_report=report,
    )


async def _validate_basic(pdf_path: Path) -> ValidationResult:
    """Basic PDF/UA check using pikepdf when veraPDF is not available."""

    def _check():
        import pikepdf

        violations = []

        with pikepdf.open(str(pdf_path)) as pdf:
            # Check MarkInfo
            mark_info = pdf.Root.get("/MarkInfo")
            if not mark_info or not mark_info.get("/Marked"):
                violations.append(Violation(
                    rule_id="basic-1",
                    description="PDF is not marked as tagged (missing MarkInfo/Marked)",
                    severity="error",
                ))

            # Check Language
            if "/Lang" not in pdf.Root:
                violations.append(Violation(
                    rule_id="basic-2",
                    description="Document language not set",
                    severity="error",
                ))

            # Check StructTreeRoot
            if "/StructTreeRoot" not in pdf.Root:
                violations.append(Violation(
                    rule_id="basic-3",
                    description="No structure tree (StructTreeRoot missing)",
                    severity="error",
                ))

            # Check title
            info = pdf.Root.get("/Info")
            if not info or "/Title" not in info:
                violations.append(Violation(
                    rule_id="basic-4",
                    description="Document title not set in metadata",
                    severity="warning",
                ))

        compliant = len([v for v in violations if v.severity == "error"]) == 0

        return ValidationResult(
            compliant=compliant,
            violations=violations,
            raw_report={"validator": "basic-pikepdf", "checks": len(violations)},
        )

    return await asyncio.to_thread(_check)
