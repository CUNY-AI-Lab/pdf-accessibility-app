"""Normalize PDF font names and identify OCR overlay fonts."""

import re

FONT_SUBSET_RE = re.compile(r"^[A-Z]{6}\+.+")


FONT_NAME_RE = re.compile(r"[^A-Za-z0-9]+")


def _strip_subset_prefix(font_name: str | None) -> str:
    raw = str(font_name or "").strip().lstrip("/")
    if FONT_SUBSET_RE.match(raw):
        return raw.split("+", 1)[1]
    return raw


def _normalize_font_name(font_name: str | None) -> str:
    return FONT_NAME_RE.sub("", _strip_subset_prefix(font_name)).lower()


def _looks_like_ocr_overlay_font(font_name: str | None) -> bool:
    normalized = _normalize_font_name(font_name)
    if not normalized:
        return False
    return any(token in normalized for token in ("ocr", "glyphless", "hidden"))


def _base_font_name(font_dict) -> str:
    base_font = font_dict.get("/BaseFont")
    if base_font:
        return _strip_subset_prefix(str(base_font))
    return ""
