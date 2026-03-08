import re
import unicodedata
from typing import Any

SPACED_LETTER_PATTERN = re.compile(r"(?:\b[\w&]\s+){4,}[\w&]\b")


def normalize_visible_text(value: Any) -> str:
    return " ".join(str(value or "").split())


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

    has_latin_letter = False
    has_non_latin_letter = False
    for char in normalized:
        if not char.isalpha():
            continue
        try:
            char_name = unicodedata.name(char)
        except ValueError:
            continue
        if "LATIN" in char_name:
            has_latin_letter = True
        else:
            has_non_latin_letter = True
        if has_latin_letter and has_non_latin_letter:
            signals.append("mixed scripts in one text block")
            break

    repeated_internal_spacing = re.search(r"[A-Za-z]\s{2,}[A-Za-z]", normalized)
    if repeated_internal_spacing:
        signals.append("irregular internal spacing")

    return signals


def looks_suspicious_text(text: Any) -> bool:
    return len(suspicious_text_signals(text)) > 0
