"""Parse positioned content regions, text runs and Form XObject invocations."""

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any

import pikepdf

from app.pipeline.page_glyphs import MeasuredGlyph, MeasuredTextOp, union_bbox
from app.pipeline.tagger_geometry import (
    IDENTITY,
    TEXT_SHOW_OPERATORS,
    ContentTarget,
    _bbox_center,
    _bbox_from_center,
    _bbox_from_points,
    _coerce_int,
    _extract_text_from_operands,
    _glyph_key,
    _lookup_xobject,
    _mat_multiply,
    _matrix_tuple,
    _normalize_text,
    _rect_array_to_bbox,
    _safe_float,
    _transform_point,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Content region extraction — positions from content streams
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ContentRegion:
    """A region in the content stream with its computed page position."""
    kind: str  # "text", "image", or "passthrough"
    start_idx: int  # index into instructions list
    end_idx: int  # exclusive end index
    instructions: list  # the actual instructions
    cx: float = 0.0  # center x in page coordinates
    cy: float = 0.0  # center y in page coordinates
    width: float = 0.0
    height: float = 0.0
    xobject_name: str = ""  # for image regions
    bbox: dict[str, float] | None = None  # {l,b,r,t}
    text: str = ""
    ctm: tuple[float, float, float, float, float, float] = IDENTITY


@dataclass
class TextShowRun:
    """A positioned text-showing operation inside a text object."""

    instruction_idx: int
    segment_start_idx: int
    segment_end_idx: int
    cx: float
    cy: float
    bbox: dict[str, float]
    text: str = ""
    render_mode: int = 0


@dataclass
class FormInvocation:
    """A Form XObject invocation preserved in its caller and tagged inside the form."""

    xobject_name: str
    stream: pikepdf.Object
    start_idx: int
    end_idx: int
    instructions: list
    initial_ctm: tuple[float, float, float, float, float, float] = IDENTITY


def _update_text_pos(pos, first_text_pos, last_text_pos):
    """Track first and last text positions in a block."""
    if first_text_pos is None:
        first_text_pos = pos
    last_text_pos = pos
    return first_text_pos, last_text_pos


def _extract_content_regions(
    instructions: list,
    content_owner,
    *,
    initial_ctm: tuple[float, float, float, float, float, float] = IDENTITY,
) -> list[ContentRegion]:
    """Walk a content stream tracking graphics state to extract positioned regions.

    Returns a list of ContentRegion objects, each representing either a text
    block (BT..ET) or an image placement (Do), with approximate page coordinates
    computed from the CTM and text matrix.
    """
    regions = []
    ctm = initial_ctm
    ctm_stack = []
    font_size = 12.0

    i = 0
    while i < len(instructions):
        instr = instructions[i]
        op = str(instr.operator)

        # ── Graphics state operators ──
        if op == "q":
            ctm_stack.append(ctm)
        elif op == "Q":
            if ctm_stack:
                ctm = ctm_stack.pop()
        elif op == "cm":
            operands = list(instr.operands) if hasattr(instr, "operands") else []
            if len(operands) >= 6:
                m = tuple(_safe_float(x) for x in operands[:6])
                ctm = _mat_multiply(m, ctm)

        # ── Text block ──
        elif op == "BT":
            bt_start = i
            bt_block = [instr]
            text_matrix = IDENTITY
            first_text_pos = None
            last_text_pos = None
            block_font_size = font_size
            text_chunks: list[str] = []
            xs: list[float] = []
            ys: list[float] = []
            i += 1

            while i < len(instructions):
                inner = instructions[i]
                inner_op = str(inner.operator)
                bt_block.append(inner)

                if inner_op == "ET":
                    i += 1
                    break

                inner_ops = list(inner.operands) if hasattr(inner, "operands") else []

                if inner_op == "Tm" and len(inner_ops) >= 6:
                    text_matrix = tuple(_safe_float(x) for x in inner_ops[:6])
                    pos = _transform_point(ctm, text_matrix[4], text_matrix[5])
                    xs.append(pos[0])
                    ys.append(pos[1])
                    first_text_pos, last_text_pos = _update_text_pos(
                        pos, first_text_pos, last_text_pos,
                    )

                elif inner_op in ("Td", "TD") and len(inner_ops) >= 2:
                    tx, ty = _safe_float(inner_ops[0]), _safe_float(inner_ops[1])
                    text_matrix = (
                        text_matrix[0], text_matrix[1],
                        text_matrix[2], text_matrix[3],
                        text_matrix[4] + tx, text_matrix[5] + ty,
                    )
                    pos = _transform_point(ctm, text_matrix[4], text_matrix[5])
                    xs.append(pos[0])
                    ys.append(pos[1])
                    first_text_pos, last_text_pos = _update_text_pos(
                        pos, first_text_pos, last_text_pos,
                    )

                elif inner_op == "T*":
                    text_matrix = (
                        text_matrix[0], text_matrix[1],
                        text_matrix[2], text_matrix[3],
                        text_matrix[4], text_matrix[5] - block_font_size * 1.2,
                    )
                    pos = _transform_point(ctm, text_matrix[4], text_matrix[5])
                    xs.append(pos[0])
                    ys.append(pos[1])
                    first_text_pos, last_text_pos = _update_text_pos(
                        pos, first_text_pos, last_text_pos,
                    )

                elif inner_op == "Tf" and len(inner_ops) >= 2:
                    block_font_size = abs(_safe_float(inner_ops[1])) or 12.0
                    font_size = block_font_size

                elif inner_op in ("Tj", "TJ", "'", '"'):
                    if first_text_pos is None:
                        pos = _transform_point(ctm, text_matrix[4], text_matrix[5])
                        xs.append(pos[0])
                        ys.append(pos[1])
                        first_text_pos, last_text_pos = pos, pos
                    chunk = _extract_text_from_operands(inner_op, inner_ops)
                    if chunk:
                        text_chunks.append(chunk)

                i += 1

            # Compute approximate center and bbox of text block
            if first_text_pos and last_text_pos:
                cx = (first_text_pos[0] + last_text_pos[0]) / 2
                cy = (first_text_pos[1] + last_text_pos[1]) / 2
                h = abs(first_text_pos[1] - last_text_pos[1]) + block_font_size
            elif first_text_pos:
                cx, cy = first_text_pos
                h = block_font_size
            else:
                cx, cy, h = 0, 0, 0

            text = _normalize_text(" ".join(text_chunks))
            bbox = _bbox_from_points(xs, ys)
            if bbox:
                # Expand bbox to include approximate glyph extents.
                text_width_guess = max(block_font_size * 0.5 * max(len(text), 1), block_font_size * 2)
                if bbox["r"] - bbox["l"] < block_font_size * 0.5:
                    bbox["r"] = bbox["l"] + text_width_guess
                bbox["t"] += block_font_size
                bbox["b"] -= block_font_size * 0.2
            else:
                bbox = _bbox_from_center(cx, cy, block_font_size * max(len(text), 1), h or block_font_size)

            regions.append(ContentRegion(
                kind="text",
                start_idx=bt_start,
                end_idx=i,
                instructions=bt_block,
                cx=cx,
                cy=cy,
                width=max(0.0, bbox["r"] - bbox["l"]) if bbox else 0.0,
                height=max(0.0, bbox["t"] - bbox["b"]) if bbox else h,
                bbox=bbox,
                text=text,
            ))
            continue  # already incremented i

        # ── Image/XObject placement ──
        elif op == "Do":
            operands = list(instr.operands) if hasattr(instr, "operands") else []
            if operands:
                name = str(operands[0])
                is_image = False
                try:
                    resources = content_owner.get("/Resources", {})
                    xobjects = resources.get("/XObject", {})
                    xobj = xobjects.get(name)
                    if xobj is not None and hasattr(xobj, "get"):
                        is_image = xobj.get("/Subtype") == pikepdf.Name("/Image")
                except Exception:
                    pass

                if is_image:
                    x0, y0 = _transform_point(ctm, 0, 0)
                    x1, y1 = _transform_point(ctm, 1, 1)
                    bbox = {
                        "l": min(x0, x1),
                        "b": min(y0, y1),
                        "r": max(x0, x1),
                        "t": max(y0, y1),
                    }
                    regions.append(ContentRegion(
                        kind="image",
                        start_idx=i,
                        end_idx=i + 1,
                        instructions=[instr],
                        cx=(x0 + x1) / 2,
                        cy=(y0 + y1) / 2,
                        width=abs(x1 - x0),
                        height=abs(y1 - y0),
                        xobject_name=name,
                        bbox=bbox,
                        ctm=ctm,
                    ))

        i += 1

    return regions


def _page_content_bbox(content_owner) -> dict[str, float] | None:
    """Return the visible content box for a page or form stream owner."""
    for key in ("/MediaBox", "/CropBox", "/BBox"):
        try:
            bbox = _rect_array_to_bbox(content_owner.get(key))
        except Exception:
            bbox = None
        if bbox:
            return bbox
    return None


def _extract_text_show_runs(
    instructions: list,
    *,
    initial_ctm: tuple[float, float, float, float, float, float] = IDENTITY,
) -> list[TextShowRun]:
    """Extract positioned text-showing operations inside text objects.

    OCRmyPDF-style pages can contain one giant BT..ET block with hundreds of
    positioned word draws.  The coarser region extractor intentionally treats
    that as one text block; this helper preserves the finer anchors when the
    tag writer needs to recover semantic coverage.
    """
    runs: list[TextShowRun] = []
    ctm = initial_ctm
    ctm_stack = []
    text_matrix = IDENTITY
    in_text = False
    segment_start_idx = 0
    font_size = 12.0
    render_mode = 0

    for idx, instr in enumerate(instructions):
        op = str(instr.operator)
        operands = list(instr.operands) if hasattr(instr, "operands") else []

        if op == "q":
            ctm_stack.append(ctm)
        elif op == "Q":
            ctm = ctm_stack.pop() if ctm_stack else initial_ctm
        elif op == "cm" and len(operands) >= 6:
            m = tuple(_safe_float(x) for x in operands[:6])
            ctm = _mat_multiply(m, ctm)

        if op == "BT":
            in_text = True
            text_matrix = IDENTITY
            segment_start_idx = idx + 1
            continue
        if op == "ET":
            in_text = False
            segment_start_idx = idx + 1
            continue
        if not in_text:
            continue

        if op == "Tf" and len(operands) >= 2:
            font_size = abs(_safe_float(operands[1])) or font_size
        elif op == "Tr" and operands:
            render_mode = _coerce_int(operands[0], default=render_mode)
        elif op == "Tm" and len(operands) >= 6:
            text_matrix = tuple(_safe_float(x) for x in operands[:6])
        elif op in ("Td", "TD") and len(operands) >= 2:
            tx, ty = _safe_float(operands[0]), _safe_float(operands[1])
            text_matrix = (
                text_matrix[0], text_matrix[1],
                text_matrix[2], text_matrix[3],
                text_matrix[4] + tx, text_matrix[5] + ty,
            )
        elif op == "T*":
            text_matrix = (
                text_matrix[0], text_matrix[1],
                text_matrix[2], text_matrix[3],
                text_matrix[4], text_matrix[5] - font_size * 1.2,
            )
        elif op in TEXT_SHOW_OPERATORS:
            text = _extract_text_from_operands(op, operands)
            x, y = _transform_point(ctm, text_matrix[4], text_matrix[5])
            width_guess = max(font_size * 0.45 * max(len(text.strip()), 1), font_size)
            p0 = _transform_point(ctm, text_matrix[4], text_matrix[5] - font_size * 0.25)
            p1 = _transform_point(ctm, text_matrix[4] + width_guess, text_matrix[5] + font_size * 1.05)
            bbox = {
                "l": min(p0[0], p1[0]),
                "b": min(p0[1], p1[1]),
                "r": max(p0[0], p1[0]),
                "t": max(p0[1], p1[1]),
            }
            runs.append(TextShowRun(
                instruction_idx=idx,
                segment_start_idx=segment_start_idx,
                segment_end_idx=idx + 1,
                cx=x,
                cy=y,
                bbox=bbox,
                text=text,
                render_mode=render_mode,
            ))
            segment_start_idx = idx + 1

    return runs


def _measured_by_instruction(
    instructions: list, measured_ops: list[MeasuredTextOp] | None
) -> dict[int, MeasuredTextOp] | None:
    """Pair each text-showing instruction with pdfminer's measurement of it
    (see page_glyphs). None, with a warning, when the stream was not measured
    or its text operators do not line up one to one with the measured ones;
    the estimated positions then stand."""
    op_indices = [
        idx for idx, instr in enumerate(instructions) if str(instr.operator) in TEXT_SHOW_OPERATORS
    ]
    if measured_ops is None or len(op_indices) != len(measured_ops):
        logger.warning(
            "No measured text for %s text operators (%s measured); using estimated positions",
            len(op_indices),
            "none" if measured_ops is None else len(measured_ops),
        )
        return None
    return dict(zip(op_indices, measured_ops, strict=True))


def _split_text_ops_across_targets(
    instructions: list,
    measured: dict[int, MeasuredTextOp],
    word_owners: dict[tuple, ContentTarget | None],
) -> tuple[list, dict[int, MeasuredTextOp], list[int]]:
    """Split each Tj or TJ whose glyphs have more than one owner (see
    _word_owners) into consecutive TJs, one per owner, so each can be tagged
    on its own. A string is cut between glyph codes where the owner changes,
    as opendataloader-pdf does for a string that crosses table cells; a
    number (a position adjustment) stays with the string before it.
    Rendering is unchanged: the text position carries over from one TJ to
    the next.

    Returns the new instructions, their measurements, and for each original
    instruction index (and one past the end) its new index, so positions
    found before the split can be moved."""
    new_instructions: list = []
    new_measured: dict[int, MeasuredTextOp] = {}
    new_index: list[int] = []
    for idx, instr in enumerate(instructions):
        new_index.append(len(new_instructions))
        op = measured.get(idx)
        operator = str(instr.operator)
        owners = (
            [word_owners.get(_glyph_key(glyph)) for glyph in op.glyphs]
            if op is not None and operator in TEXT_SHOW_OPERATORS and instr.operands
            else []
        )
        if len(set(owners)) < 2:
            if op is not None:
                new_measured[len(new_instructions)] = op
            new_instructions.append(instr)
            continue
        # The quote operators also show text, after moving to the next
        # line (and, for double quote, setting word and character spacing).
        # Expand their state changes once before splitting the string.
        if operator == '"':
            for operand, name in zip(list(instr.operands)[:2], ("Tw", "Tc"), strict=True):
                new_instructions.append(pikepdf.ContentStreamInstruction(
                    [operand], pikepdf.Operator(name)
                ))
        if operator in {"'", '"'}:
            new_instructions.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*")))
        array = list(instr.operands[0]) if operator == "TJ" else [instr.operands[-1]]
        drawn: dict[int, list[tuple[MeasuredGlyph, ContentTarget | None]]] = {}
        for glyph, owner in zip(op.glyphs, owners, strict=True):
            drawn.setdefault(glyph.element, []).append((glyph, owner))
        # The array in pieces: a number or undrawn string as it is, a drawn
        # string cut where its glyphs' owner changes. Each piece keeps its
        # glyphs, their code spans moved to the piece's start.
        pieces: list[tuple[object, list[MeasuredGlyph], ContentTarget | None]] = []
        for element_index, item in enumerate(array):
            glyphs = drawn.get(element_index)
            if not glyphs:
                pieces.append((item, [], None))
                continue
            data = bytes(item)
            cut, run, run_owner = 0, [], glyphs[0][1]
            for glyph, owner in glyphs:
                if owner != run_owner:
                    pieces.append((pikepdf.String(data[cut : glyph.code[0]]), run, run_owner))
                    cut, run, run_owner = glyph.code[0], [], owner
                start, stop = glyph.code
                run.append(dataclasses.replace(glyph, code=(start - cut, stop - cut)))
            pieces.append((pikepdf.String(data[cut:]), run, run_owner))
        groups: list[list[tuple[object, list[MeasuredGlyph], ContentTarget | None]]] = [[]]
        group_owner: ContentTarget | None = None
        for piece in pieces:
            _, glyphs, owner = piece
            if glyphs and any(member[1] for member in groups[-1]) and owner != group_owner:
                groups.append([])
            groups[-1].append(piece)
            if glyphs:
                group_owner = owner
        for group in groups:
            new_measured[len(new_instructions)] = MeasuredTextOp(
                glyphs=[
                    dataclasses.replace(glyph, element=position)
                    for position, (_, glyphs, _) in enumerate(group)
                    for glyph in glyphs
                ]
            )
            new_instructions.append(
                pikepdf.ContentStreamInstruction(
                    [pikepdf.Array([item for item, _, _ in group])], pikepdf.Operator("TJ")
                )
            )
    new_index.append(len(new_instructions))
    return new_instructions, new_measured, new_index


def _apply_measured_geometry(
    regions: list[ContentRegion],
    runs: list[TextShowRun],
    measured: dict[int, MeasuredTextOp],
) -> None:
    """Give text runs and text regions the text and positions pdfminer
    measured in place of the estimates above."""
    for run in runs:
        op = measured.get(run.instruction_idx)
        if op is None or (bbox := op.bbox) is None:
            continue
        run.bbox = bbox
        run.cx, run.cy = _bbox_center(bbox)
        run.text = op.text

    for region in regions:
        if region.kind != "text":
            continue
        inside = [
            op
            for idx, op in sorted(measured.items())
            if region.start_idx <= idx < region.end_idx and op.glyphs
        ]
        if not inside:
            continue
        bbox = union_bbox(op.bbox for op in inside)
        region.bbox = bbox
        region.cx, region.cy = _bbox_center(bbox)
        region.width = bbox["r"] - bbox["l"]
        region.height = bbox["t"] - bbox["b"]
        region.text = _normalize_text(" ".join(op.text for op in inside))


def _text_runs_look_like_hidden_ocr_layer(runs: list[TextShowRun]) -> bool:
    if not runs:
        return False
    hidden = sum(1 for run in runs if run.render_mode == 3)
    return hidden / max(len(runs), 1) >= 0.8


def _is_ocr_form_xobject(name: str, xobject: Any) -> bool:
    return (
        name.startswith("/OCR")
        and isinstance(xobject, pikepdf.Stream)
        and xobject.get("/Subtype") == "/Form"
    )


def _extract_form_invocations(content_owner, instructions: list) -> list[FormInvocation]:
    """Form calls and their transforms, regardless of the producer's names.

    Only the Do passes through: q/Q and other graphics state still belong
    to the caller. Nested forms are visited by the tagging traversal.
    """
    resources = content_owner.get("/Resources", {})
    invocations: list[FormInvocation] = []
    ctm = IDENTITY
    stack: list[tuple] = []
    for index, instr in enumerate(instructions):
        op = str(instr.operator)
        operands = list(instr.operands) if op in {"cm", "Do"} else []
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            if stack:
                ctm = stack.pop()
        elif op == "cm" and len(operands) >= 6:
            ctm = _mat_multiply(tuple(_safe_float(x) for x in operands[:6]), ctm)
        elif op == "Do" and operands:
            name = str(operands[0])
            form = _lookup_xobject(resources, name)
            if isinstance(form, pikepdf.Stream) and form.get("/Subtype") == "/Form":
                invocations.append(FormInvocation(
                    xobject_name=name,
                    stream=form,
                    start_idx=index,
                    end_idx=index + 1,
                    instructions=[instr],
                    initial_ctm=_mat_multiply(_matrix_tuple(form.get("/Matrix")), ctm),
                ))
    return invocations


# ──────────────────────────────────────────────────────────────────────────────
# Content stream rewriting — position-based
# ──────────────────────────────────────────────────────────────────────────────

def _strip_existing_markers(instructions: list, resources=None) -> list:
    """Discard old tagging IDs, retaining the source's text replacements.

    /ActualText is part of how a PDF encodes text (ISO 32000-1, 14.9.4),
    including fonts whose glyph codes have no Unicode mapping. Removing it
    along with the old MCIDs changes what the page says.
    """
    stripped = []
    retained: list[bool] = []
    properties = resources.get("/Properties", {}) if resources is not None else {}
    for instr in instructions:
        op = str(instr.operator)
        if op in {"BDC", "BMC"}:
            props = instr.operands[1] if op == "BDC" and len(instr.operands) > 1 else None
            if isinstance(props, pikepdf.Name):
                props = properties.get(props)
            keep = isinstance(props, pikepdf.Dictionary) and "/ActualText" in props
            retained.append(keep)
            if keep:
                stripped.append(pikepdf.ContentStreamInstruction(
                    [pikepdf.Name("/Span"), pikepdf.Dictionary(ActualText=props.ActualText)],
                    pikepdf.Operator("BDC"),
                ))
        elif op == "EMC":
            if retained and retained.pop():
                stripped.append(instr)
        else:
            stripped.append(instr)
    return stripped
