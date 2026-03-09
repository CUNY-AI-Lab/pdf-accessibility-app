import re
import unicodedata
from pathlib import Path
from typing import Any

import regex
from ftfy import fix_text
from rapidfuzz import fuzz

SPACED_LETTER_PATTERN = re.compile(r"(?:\b[\w&]\s+){4,}[\w&]\b")
LETTER_RE = regex.compile(r"\A\p{Letter}\Z")
LATIN_SCRIPT_RE = regex.compile(r"\A\p{Script=Latin}\Z")
GREEK_SCRIPT_RE = regex.compile(r"\A\p{Script=Greek}\Z")
BENIGN_SCRIPT_RE = regex.compile(r"\A(?:\p{Script=Common}|\p{Script=Inherited})\Z")
GROUNDED_TEXT_OCR_MIN_ALNUM = 3


def _script_bucket(char: str) -> str | None:
    if not LETTER_RE.match(char):
        return None
    try:
        char_name = unicodedata.name(char)
    except ValueError:
        return None
    if BENIGN_SCRIPT_RE.match(char):
        return "BENIGN"
    if "MATHEMATICAL" in char_name:
        if "LATIN" in char_name:
            return "LATIN"
        if "GREEK" in char_name or "COPTIC" in char_name:
            return "GREEK"
        return "BENIGN"
    if LATIN_SCRIPT_RE.match(char):
        return "LATIN"
    if GREEK_SCRIPT_RE.match(char):
        return "GREEK"
    return "OTHER"


def normalize_visible_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def repair_text_candidate(value: Any) -> str:
    return normalize_visible_text(fix_text(str(value or "")))


def text_similarity_score(left: Any, right: Any) -> float:
    left_text = repair_text_candidate(left)
    right_text = repair_text_candidate(right)
    if not left_text and not right_text:
        return 1.0
    if not left_text or not right_text:
        return 0.0
    return float(fuzz.ratio(left_text, right_text)) / 100.0


def _normalize_text(value: str | None) -> str:
    raw = str(value or "").lower()
    return " ".join(regex.sub(r"[^a-z0-9]+", " ", raw).split())


def _normalize_dense_text(value: str | None) -> str:
    return _normalize_text(value).replace(" ", "")


def suspicious_text_signals(text: Any) -> list[str]:
    normalized = normalize_visible_text(text)
    if not normalized:
        return []

    signals: list[str] = []

    if SPACED_LETTER_PATTERN.search(normalized):
        signals.append("letters separated by spaces")

    alpha_tokens = re.findall(r"[A-Za-z]+", normalized)
    if len(alpha_tokens) >= 6:
        average_length = sum(len(token) for token in alpha_tokens) / max(len(alpha_tokens), 1)
        if average_length <= 1.6:
            signals.append("very short token pattern")

    script_counts: dict[str, int] = {}
    for char in normalized:
        if not char.isalpha():
            continue
        bucket = _script_bucket(char)
        if bucket is None:
            continue
        script_counts[bucket] = script_counts.get(bucket, 0) + 1
    # Do not flag ordinary statistical notation that mixes Latin text with a
    # few Greek symbols. We only treat scripts outside the Latin/Greek family
    # as suspicious within otherwise Latin text.
    if script_counts.get("LATIN") and script_counts.get("OTHER"):
        signals.append("mixed scripts in one text block")

    repeated_internal_spacing = re.search(r"[A-Za-z]\s{2,}[A-Za-z]", normalized)
    if repeated_internal_spacing:
        signals.append("irregular internal spacing")

    return signals


def looks_suspicious_text(text: Any) -> bool:
    return len(suspicious_text_signals(text)) > 0


def collect_grounded_text_candidates(
    pdf_path: Path,
    structure_json: dict[str, Any],
    *,
    target_limit: int | None = None,
) -> dict[str, Any]:
    from app.services.document_intelligence import build_document_model
    from app.services.text_grounding import extract_ocr_text_from_bbox

    document = build_document_model(structure_json=structure_json)
    targets: list[dict[str, Any]] = []
    encoding_problem_count = 0

    for page in document.pages:
        for index, block in enumerate(page.blocks):
            if block.role == "artifact" or block.bbox is None:
                continue
            if (
                block.resolved_text
                and str(block.resolution_source or "").startswith("pretag_")
                and not block.semantic_blocking
            ):
                continue
            visible_text = normalize_visible_text(block.native_text_candidate or block.text)
            if len(visible_text) < 6:
                continue
            signals = suspicious_text_signals(visible_text)
            if not signals:
                continue

            bbox = block.bbox.to_dict()
            ocr_text = normalize_visible_text(
                extract_ocr_text_from_bbox(
                    pdf_path,
                    page_number=page.page_number,
                    bbox=bbox,
                )
            )
            if sum(char.isalnum() for char in ocr_text) < GROUNDED_TEXT_OCR_MIN_ALNUM:
                continue

            previous_text = ""
            next_text = ""
            previous_role = ""
            next_role = ""
            if index > 0:
                previous_block = page.blocks[index - 1]
                previous_text = normalize_visible_text(
                    previous_block.native_text_candidate or previous_block.text
                )[:240]
                previous_role = str(previous_block.role or "").strip()
            if index + 1 < len(page.blocks):
                next_block = page.blocks[index + 1]
                next_text = normalize_visible_text(
                    next_block.native_text_candidate or next_block.text
                )[:240]
                next_role = str(next_block.role or "").strip()

            ocr_dense = _normalize_dense_text(ocr_text)
            visible_dense = _normalize_dense_text(visible_text)
            if not visible_dense or not ocr_dense:
                continue
            if len(ocr_dense) > len(visible_dense) + 20 and visible_dense in ocr_dense:
                continue
            if len(ocr_dense) > max(len(visible_dense) * 2, len(visible_dense) + 40):
                previous_dense = _normalize_dense_text(previous_text)
                next_dense = _normalize_dense_text(next_text)
                if (
                    (previous_dense and previous_dense in ocr_dense)
                    or (next_dense and next_dense in ocr_dense)
                ):
                    continue

            accessible_text = normalize_visible_text(
                block.resolved_text
                or block.semantic_text_hint
                or block.native_text_candidate
                or block.text
            )
            similarity = text_similarity_score(accessible_text, ocr_text)
            if similarity >= 0.97 and "letters separated by spaces" not in signals:
                continue

            accessible_normalized = _normalize_text(accessible_text)
            ocr_normalized = _normalize_text(ocr_text)
            if not accessible_normalized or accessible_normalized == ocr_normalized:
                continue

            issue_type = (
                "spacing_only"
                if visible_dense == ocr_dense
                else "encoding_problem"
            )
            if issue_type == "encoding_problem":
                encoding_problem_count += 1

            targets.append(
                {
                    "page": page.page_number,
                    "review_id": block.review_id,
                    "role": block.role,
                    "bbox": bbox,
                    "extracted_text": block.text,
                    "native_text_candidate": accessible_text,
                    "original_text_candidate": visible_text,
                    "ocr_text_candidate": ocr_text,
                    "issue_type": issue_type,
                    "signals": signals,
                    "previous_text": previous_text,
                    "previous_role": previous_role,
                    "next_text": next_text,
                    "next_role": next_role,
                    "candidate_similarity": round(similarity, 4),
                    "native_repaired_text": repair_text_candidate(accessible_text),
                    "ocr_repaired_text": repair_text_candidate(ocr_text),
                    "risk_score": 2.0 if issue_type == "encoding_problem" else 1.0,
                    "reason": (
                        "OCR text differs materially from the accessible reading for this block."
                        if issue_type == "encoding_problem"
                        else "OCR resolves the visible text more cleanly than the spaced accessible text."
                    ),
                }
            )

    targets.sort(
        key=lambda item: (
            -float(item.get("risk_score", 0.0)),
            int(item.get("page", 0)),
            str(item.get("review_id", "")),
        )
    )
    total_targets = len(targets)
    if isinstance(target_limit, int) and target_limit >= 0:
        targets = targets[:target_limit]
    return {
        "target_count": total_targets,
        "encoding_problem_count": encoding_problem_count,
        "targets": targets,
    }
