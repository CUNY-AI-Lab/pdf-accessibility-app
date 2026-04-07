#!/usr/bin/env python3
"""Create a benchmark-friendly PDF by stripping accessibility semantics.

This script is intended for round-trip benchmarking:

gold accessible PDF -> stripped PDF -> remediation pipeline -> compare to gold

It is deliberately conservative about visible content. The goal is to remove
accessibility scaffolding while leaving page appearance and ordinary document
behavior intact.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pikepdf

ROOT_DROP_KEYS = frozenset({
    "/MarkInfo",
    "/Metadata",
    "/Outlines",
    "/StructTreeRoot",
    "/Lang",
})
DICT_DROP_KEYS = frozenset({
    "/ActualText",
    "/Alt",
    "/E",
    "/Lang",
    "/StructParent",
    "/StructParents",
    "/TU",
})
MARKED_CONTENT_PROPERTY_KEYS = frozenset({
    "/ActualText",
    "/Alt",
    "/E",
    "/Lang",
    "/MCID",
})
ANNOTATION_CONTENT_SUBTYPES = frozenset({
    "/Link",
    "/Widget",
})


@dataclass
class StripReport:
    root_fields_removed: int = 0
    page_fields_removed: int = 0
    dictionary_fields_removed: int = 0
    annotation_contents_removed: int = 0
    marked_content_properties_removed: int = 0
    page_streams_rewritten: int = 0
    form_streams_rewritten: int = 0
    bookmarks_removed: bool = False
    page_mode_reset: bool = False


def _resolve_object(value):
    try:
        return value.get_object()
    except Exception:
        return value


def _object_key(value) -> tuple[int, int] | None:
    resolved = _resolve_object(value)
    objgen = getattr(resolved, "objgen", None)
    if not objgen:
        return None
    if not isinstance(objgen, tuple) or len(objgen) != 2:
        return None
    return objgen


def _delete_keys(mapping, keys: frozenset[str]) -> int:
    removed = 0
    for key in keys:
        try:
            if key in mapping:
                del mapping[key]
                removed += 1
        except Exception:
            continue
    return removed


def _strip_dictionary_fields(value, report: StripReport, seen: set[tuple[int, int]]) -> None:
    resolved = _resolve_object(value)
    key = _object_key(resolved)
    if key is not None:
        if key in seen:
            return
        seen.add(key)

    if isinstance(resolved, pikepdf.Array):
        for item in resolved:
            _strip_dictionary_fields(item, report, seen)
        return

    if hasattr(resolved, "items"):
        report.dictionary_fields_removed += _delete_keys(resolved, DICT_DROP_KEYS)
        try:
            items = list(resolved.items())
        except Exception:
            return
        for _, item in items:
            _strip_dictionary_fields(item, report, seen)


def _strip_catalog(pdf: pikepdf.Pdf, root, report: StripReport, *, keep_bookmarks: bool) -> None:
    drop_keys = set(ROOT_DROP_KEYS)
    if keep_bookmarks:
        drop_keys.discard("/Outlines")
    report.root_fields_removed += _delete_keys(root, frozenset(drop_keys))

    try:
        page_mode = str(root.get("/PageMode", ""))
    except Exception:
        page_mode = ""
    if page_mode == "/UseOutlines" and not keep_bookmarks:
        try:
            del root["/PageMode"]
            report.page_mode_reset = True
        except Exception:
            pass

    if not keep_bookmarks:
        report.bookmarks_removed = True

    docinfo = getattr(pdf, "docinfo", None)
    if docinfo is not None:
        for key in ("/Title", "/Subject", "/Keywords"):
            try:
                if key in docinfo:
                    del docinfo[key]
                    report.root_fields_removed += 1
            except Exception:
                continue


def _strip_annotation_contents(annotation, report: StripReport, *, keep_annotation_contents: bool) -> None:
    if keep_annotation_contents:
        return
    try:
        subtype = str(annotation.get("/Subtype", ""))
    except Exception:
        subtype = ""
    if subtype not in ANNOTATION_CONTENT_SUBTYPES:
        return
    try:
        if "/Contents" in annotation:
            del annotation["/Contents"]
            report.annotation_contents_removed += 1
    except Exception:
        return


def _rewrite_marked_content(parse_target, owner, pdf: pikepdf.Pdf, report: StripReport) -> bool:
    try:
        instructions = list(pikepdf.parse_content_stream(parse_target))
    except Exception:
        return False

    changed = False
    new_instructions: list[pikepdf.ContentStreamInstruction] = []
    for instruction in instructions:
        operator_name = str(getattr(instruction, "operator", ""))
        if operator_name != "BDC":
            new_instructions.append(instruction)
            continue

        operands = list(getattr(instruction, "operands", []))
        if len(operands) != 2:
            new_instructions.append(instruction)
            continue

        tag, properties = operands
        resolved_properties = _resolve_object(properties)
        if not isinstance(resolved_properties, pikepdf.Dictionary):
            new_instructions.append(instruction)
            continue

        stripped_properties = pikepdf.Dictionary(resolved_properties)
        removed_here = _delete_keys(stripped_properties, MARKED_CONTENT_PROPERTY_KEYS)
        if removed_here <= 0:
            new_instructions.append(instruction)
            continue

        report.marked_content_properties_removed += removed_here
        changed = True
        if len(stripped_properties) == 0:
            new_instructions.append(
                pikepdf.ContentStreamInstruction([tag], pikepdf.Operator("BMC"))
            )
        else:
            new_instructions.append(
                pikepdf.ContentStreamInstruction([tag, stripped_properties], pikepdf.Operator("BDC"))
            )

    if not changed:
        return False

    stream_bytes = pikepdf.unparse_content_stream(new_instructions)
    if isinstance(owner, pikepdf.Page):
        owner["/Contents"] = pdf.make_stream(stream_bytes)
        report.page_streams_rewritten += 1
    else:
        _resolve_object(owner).write(stream_bytes)
        report.form_streams_rewritten += 1
    return True


def _rewrite_page_and_form_streams(
    page: pikepdf.Page,
    pdf: pikepdf.Pdf,
    report: StripReport,
    visited: set[tuple[int, int]],
) -> None:
    _rewrite_marked_content(page, page, pdf, report)

    resources = _resolve_object(page.get("/Resources"))
    if not hasattr(resources, "get"):
        return
    xobjects = _resolve_object(resources.get("/XObject"))
    if not hasattr(xobjects, "items"):
        return

    try:
        xobject_items = list(xobjects.items())
    except Exception:
        xobject_items = []

    for _, candidate in xobject_items:
        resolved = _resolve_object(candidate)
        key = _object_key(resolved)
        if key is not None and key in visited:
            continue
        if key is not None:
            visited.add(key)
        try:
            subtype = str(resolved.get("/Subtype", ""))
        except Exception:
            subtype = ""
        if subtype != "/Form":
            continue
        _rewrite_marked_content(resolved, resolved, pdf, report)
        nested_resources = _resolve_object(resolved.get("/Resources"))
        if hasattr(nested_resources, "get"):
            nested_xobjects = _resolve_object(nested_resources.get("/XObject"))
            if hasattr(nested_xobjects, "items"):
                try:
                    nested_items = list(nested_xobjects.items())
                except Exception:
                    nested_items = []
                for _, nested_candidate in nested_items:
                    nested_resolved = _resolve_object(nested_candidate)
                    nested_key = _object_key(nested_resolved)
                    if nested_key is not None and nested_key in visited:
                        continue
                    if nested_key is not None:
                        visited.add(nested_key)
                    try:
                        nested_subtype = str(nested_resolved.get("/Subtype", ""))
                    except Exception:
                        nested_subtype = ""
                    if nested_subtype == "/Form":
                        _rewrite_marked_content(nested_resolved, nested_resolved, pdf, report)


def strip_accessibility(
    *,
    input_path: Path,
    output_path: Path,
    keep_bookmarks: bool = False,
    keep_annotation_contents: bool = False,
) -> StripReport:
    report = StripReport()
    with pikepdf.open(str(input_path), allow_overwriting_input=True) as pdf:
        _strip_catalog(pdf, pdf.Root, report, keep_bookmarks=keep_bookmarks)

        _strip_dictionary_fields(pdf.Root, report, set())

        visited_form_streams: set[tuple[int, int]] = set()
        for page in pdf.pages:
            report.page_fields_removed += _delete_keys(page, frozenset({"/Tabs", "/StructParents"}))
            _strip_dictionary_fields(page, report, set())
            annots = page.get("/Annots")
            if isinstance(annots, pikepdf.Array):
                for annot in annots:
                    annotation = _resolve_object(annot)
                    if not hasattr(annotation, "get"):
                        continue
                    _strip_annotation_contents(
                        annotation,
                        report,
                        keep_annotation_contents=keep_annotation_contents,
                    )
            _rewrite_page_and_form_streams(page, pdf, report, visited_form_streams)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(str(output_path))

    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Path to the gold accessible PDF.")
    parser.add_argument("--output", required=True, type=Path, help="Path to write the stripped PDF.")
    parser.add_argument(
        "--keep-bookmarks",
        action="store_true",
        help="Keep outline/bookmark navigation in the stripped file.",
    )
    parser.add_argument(
        "--keep-annotation-contents",
        action="store_true",
        help="Keep non-visible link/widget /Contents values instead of stripping them.",
    )
    parser.add_argument(
        "--summary-json",
        type=Path,
        help="Optional path for a machine-readable strip summary.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = strip_accessibility(
        input_path=args.input,
        output_path=args.output,
        keep_bookmarks=args.keep_bookmarks,
        keep_annotation_contents=args.keep_annotation_contents,
    )
    summary = asdict(report)
    print(json.dumps(summary, indent=2))
    if args.summary_json is not None:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
