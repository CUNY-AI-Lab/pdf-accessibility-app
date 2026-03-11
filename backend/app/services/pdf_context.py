from __future__ import annotations

import re
from typing import Any

PAGE_SEGMENT_RE = re.compile(r"pages\[(\d+)\]")
ANNOT_SEGMENT_RE = re.compile(r"(?:annotations|annots)\[(\d+)\]")
XOBJECT_SEGMENT_RE = re.compile(r"xObject\[(\d+)\](?:\(([^)]*)\))?")
APPEARANCE_SEGMENT_RE = re.compile(r"appearanceStream\[(\d+)\]")
CONTENT_STREAM_SEGMENT_RE = re.compile(r"contentStream\[(\d+)\]")
OPERATOR_SEGMENT_RE = re.compile(r"operators\[(\d+)\]")


def parse_verapdf_context_path(context_path: str) -> dict[str, Any]:
    segments = [segment for segment in str(context_path or "").split("/") if segment]
    parsed: dict[str, Any] = {
        "page_number": None,
        "annotation_index": None,
        "appearance_index": None,
        "operator_index": None,
        "page_content_stream_index": None,
        "xobject_chain": [],
    }
    pending_operator_index: int | None = None
    pending_xobject_entry: dict[str, Any] | None = None

    for segment in segments:
        if match := PAGE_SEGMENT_RE.search(segment):
            parsed["page_number"] = int(match.group(1)) + 1
            continue
        if match := ANNOT_SEGMENT_RE.search(segment):
            parsed["annotation_index"] = int(match.group(1))
            continue
        if match := XOBJECT_SEGMENT_RE.search(segment):
            raw_name = (match.group(2) or "").strip()
            if raw_name and " " in raw_name:
                raw_name = raw_name.split(" ", 1)[0]
            pending_xobject_entry = {
                "index": int(match.group(1)),
                "name": raw_name.lstrip("/"),
                "from_operator_index": pending_operator_index,
                "content_stream_index": None,
            }
            parsed["xobject_chain"].append(pending_xobject_entry)
            pending_operator_index = None
            continue
        if match := APPEARANCE_SEGMENT_RE.search(segment):
            parsed["appearance_index"] = int(match.group(1))
            continue
        if match := CONTENT_STREAM_SEGMENT_RE.search(segment):
            if pending_xobject_entry is not None:
                pending_xobject_entry["content_stream_index"] = int(match.group(1))
            elif parsed["annotation_index"] is None and parsed["appearance_index"] is None:
                parsed["page_content_stream_index"] = int(match.group(1))
            continue
        if match := OPERATOR_SEGMENT_RE.search(segment):
            operator_index = int(match.group(1))
            parsed["operator_index"] = operator_index
            pending_operator_index = operator_index
            continue

    return parsed
