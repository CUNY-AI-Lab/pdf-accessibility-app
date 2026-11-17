"""Find the codes each simple font shows, and the fonts no reader can see.

Some searchable scans paint their recognized text first and the page image
after it, over the whole page: ABBYY FineReader's "text under the page image"
output and Acrobat Paper Capture's. The image covers the text, so its glyph
shapes never appear. A font whose every use is covered that way, or is in
rendering mode 3, or shows only space characters, renders the same with an
empty glyph for each code. ``glyphless_program`` builds such a program, as
Tesseract's GlyphLessFont does for OCRmyPDF's text layer.

Only an image drawn in a page's own content stream counts as a cover, and
only when nothing can let the text show through: the image has no mask, soft
mask or JPEG 2000 opacity channel, its placement fills a rectangle containing
the page's visible area, no clipping path is in effect, the graphics state is
fully opaque with the Normal blend mode and no overprint, and neither the
image nor its marked content is optional content. Text in annotation appearances, tiling
patterns, Type 3 glyphs and soft masks is visible unless it is in mode 3.
Fonts in AcroForm default resources are visible, and their codes unknown: a
viewer can show any code in a field.
"""

from dataclasses import dataclass, field
from io import BytesIO

import pikepdf

from app.pipeline.fonts.encoding import _glyph_name_for_code
from app.pipeline.fonts.invisible import (
    _EPOCH_2000,
    INVISIBLE,
    TEXT_SHOWING,
    _dictionary,
    _render_mode,
)
from app.pipeline.fonts.resources import TOUNICODE_RESOURCES, _iter_pdf_resource_roots, _obj_key

SIMPLE_SUBTYPES = frozenset({"/TrueType", "/Type1", "/MMType1"})
# Glyph names whose glyph is blank in any font: space and no-break space.
SPACE_NAMES = frozenset({"space", "nbspace", "nonbreakingspace", "uni0020", "uni00A0"})
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
# A placed scan can fall a fraction of a point short of the page edge.
_COVER_TOLERANCE = 1.0


def _multiply(m, n):
    """The matrix product m x n, as PDF composes `cm` with the CTM."""
    a, b, c, d, e, f = m
    p, q, r, s, t, u = n
    return (
        a * p + b * r,
        a * q + b * s,
        c * p + d * r,
        c * q + d * s,
        e * p + f * r + t,
        e * q + f * s + u,
    )


def _numbers(values) -> tuple[float, ...] | None:
    try:
        return tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return None


def _visible_box(page) -> tuple[float, float, float, float] | None:
    """The intersection of the page's (inherited) MediaBox and CropBox."""
    boxes = []
    for box in (page.mediabox, page.cropbox):
        values = _numbers(box)
        if values is None or len(values) != 4:
            return None
        x0, y0, x1, y1 = values
        boxes.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
    x0, y0 = max(box[0] for box in boxes), max(box[1] for box in boxes)
    x1, y1 = min(box[2] for box in boxes), min(box[3] for box in boxes)
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def _opaque_image(image) -> bool:
    if not isinstance(image, pikepdf.Stream) or image.get("/Subtype") != "/Image":
        return False
    if image.get("/ImageMask") is True or "/OC" in image:
        return False
    if "/SMask" in image or "/Mask" in image:
        return False
    try:
        return int(image.get("/SMaskInData", 0) or 0) == 0
    except (TypeError, ValueError):
        return False


def _covers(ctm, box) -> bool:
    """The image's unit square, placed by `ctm`, fills a rectangle containing `box`."""
    a, b, c, d, e, f = ctm
    scale = max(abs(a), abs(b), abs(c), abs(d))
    if scale <= 0:
        return False
    flat = scale * 1e-6
    if not ((abs(b) <= flat and abs(c) <= flat) or (abs(a) <= flat and abs(d) <= flat)):
        return False
    xs = (e, e + a, e + c, e + a + c)
    ys = (f, f + b, f + d, f + b + d)
    x0, y0, x1, y1 = box
    return (
        min(xs) <= x0 + _COVER_TOLERANCE
        and min(ys) <= y0 + _COVER_TOLERANCE
        and max(xs) >= x1 - _COVER_TOLERANCE
        and max(ys) >= y1 - _COVER_TOLERANCE
    )


@dataclass
class _State:
    font: object = None
    render: int = 0
    ctm: tuple[float, ...] = _IDENTITY
    clipped: bool = False
    fill_alpha: float = 1.0
    blend_normal: bool = True
    soft_mask: bool = False
    overprint: bool = False

    def copy(self) -> "_State":
        return _State(**self.__dict__)

    @property
    def opaque(self) -> bool:
        return (
            self.fill_alpha >= 1.0
            and self.blend_normal
            and not self.soft_mask
            and not self.overprint
        )


@dataclass
class SimpleFontUses:
    """Codes shown by each indirect simple font, and the fonts a reader may see."""

    codes: dict = field(default_factory=dict)
    visible: set = field(default_factory=set)
    unknown_codes: set = field(default_factory=set)

    def shown_codes(self, font) -> set[int] | None:
        """Every code the font shows, or None when that cannot be known."""
        key = _obj_key(font)
        if key is None or key in self.unknown_codes:
            return None
        return set(self.codes.get(key, ()))

    def unseen_codes(self, font) -> set[int] | None:
        """The codes of a font no reader can see, or None if any may be seen.

        Every use is covered, in rendering mode 3, or a space.
        """
        codes = self.shown_codes(font)
        if not codes:
            return None
        if _obj_key(font) not in self.visible:
            return codes
        for code in codes:
            named = _glyph_name_for_code(font, code, None)
            if named is None or named[0] not in SPACE_NAMES:
                return None
        return codes


@dataclass(frozen=True)
class _Show:
    font: pikepdf.Dictionary
    codes: frozenset[int]
    render: int


class _Walker:
    def __init__(self):
        self.uses = SimpleFontUses()
        # Paint order on the current page: each show's sequence number and
        # font, and the sequence number of the last image covering the page.
        self.sequence = 0
        self.page_shows: list[tuple[int, tuple[int, int]]] = []
        self.last_cover = -1
        self._extras: set = set()
        # A Form XObject's shows by entry font, mode and resources, replayed
        # in paint order when it is drawn again.
        self._forms: dict = {}
        self._captures: list[list[_Show]] = []

    def record(self, show: _Show, *, coverable: bool) -> None:
        key = _obj_key(show.font)
        if key is None:
            return
        self.uses.codes.setdefault(key, set()).update(show.codes)
        if self._captures:
            self._captures[-1].append(show)
        self.sequence += 1
        if show.render == INVISIBLE:
            return
        if coverable:
            self.page_shows.append((self.sequence, key))
        else:
            self.uses.visible.add(key)

    def show(self, font, render, op, operands, *, coverable: bool) -> None:
        if not isinstance(font, pikepdf.Dictionary) or font.get("/Subtype") not in SIMPLE_SUBTYPES:
            return
        values = operands[0] if op == "TJ" and operands else operands[-1:]
        if not isinstance(values, (pikepdf.Array, list)):
            return
        codes = frozenset(
            code for value in values if isinstance(value, pikepdf.String) for code in bytes(value)
        )
        self.record(_Show(font, codes, render), coverable=coverable)

    def walk_extras(self, resources, ancestors) -> None:
        """Streams reached through resources rather than Do: never covered."""
        key = _obj_key(resources)
        if key is not None:
            if key in self._extras:
                return
            self._extras.add(key)
        for _, pattern in (_dictionary(resources.get("/Pattern")) or {}).items():
            if isinstance(pattern, pikepdf.Stream) and pattern.get("/PatternType") == 1:
                self.walk(pattern, resources, _State(), ancestors, coverable=False)
        for _, font in (_dictionary(resources.get("/Font")) or {}).items():
            if isinstance(font, pikepdf.Dictionary) and font.get("/Subtype") == "/Type3":
                own = _dictionary(font.get("/Resources")) or resources
                for _, proc in (_dictionary(font.get("/CharProcs")) or {}).items():
                    if isinstance(proc, pikepdf.Stream):
                        self.walk(proc, own, _State(), ancestors, coverable=False)
        for _, state in (_dictionary(resources.get("/ExtGState")) or {}).items():
            mask = (
                _dictionary(state.get("/SMask")) if isinstance(state, pikepdf.Dictionary) else None
            )
            group = mask.get("/G") if mask is not None else None
            if isinstance(group, pikepdf.Stream):
                self.walk(group, resources, _State(), ancestors, coverable=False)

    def form(self, form, inherited, state, ancestors, *, coverable: bool) -> None:
        own = _dictionary(form.get("/Resources"))
        scope = "own" if own is not None else (_obj_key(inherited) or id(inherited))
        memo = (form.objgen, _obj_key(state.font), state.render, scope, coverable)
        if memo in self._forms:
            for show in self._forms[memo]:
                self.record(show, coverable=coverable)
            return
        self._captures.append([])
        try:
            self.walk(form, inherited, state, ancestors, coverable=coverable)
        finally:
            shows = self._captures.pop()
        self._forms[memo] = shows
        if self._captures:
            self._captures[-1].extend(shows)

    def walk(self, owner, inherited, state, ancestors=frozenset(), *, coverable, box=None):
        """Walk one content stream in paint order; it uses its own resources if it has them.

        Only a page's own stream (one with a `box`) tracks the CTM and covers.
        """
        obj = owner.obj if isinstance(owner, pikepdf.Page) else owner
        key = obj.objgen
        if key in ancestors:
            return
        ancestors = ancestors | {key}
        resources = _dictionary(obj.get("/Resources"))
        if resources is None:
            resources = _dictionary(inherited)
        if resources is None:
            resources = pikepdf.Dictionary()
        self.walk_extras(resources, ancestors)
        fonts = _dictionary(resources.get("/Font")) or {}
        try:
            instructions = pikepdf.parse_content_stream(owner)
        except (pikepdf.PdfError, ValueError, TypeError):
            # Unread content may show any of its fonts, any code.
            for _, font in fonts.items():
                if (font_key := _obj_key(font)) is not None:
                    self.uses.visible.add(font_key)
                    self.uses.unknown_codes.add(font_key)
            return
        states = _dictionary(resources.get("/ExtGState")) or {}
        xobjects = _dictionary(resources.get("/XObject")) or {}
        stack: list[_State] = []
        optional: list[bool] = []
        state = state.copy()
        for instruction in instructions:
            op, operands = str(instruction.operator), list(instruction.operands)
            if op == "q":
                stack.append(state.copy())
            elif op == "Q" and stack:
                state = stack.pop()
            elif op == "cm" and box is not None:
                matrix = _numbers(operands)
                if matrix is not None and len(matrix) == 6:
                    state.ctm = _multiply(matrix, state.ctm)
                else:
                    state.clipped = True
            elif op in ("W", "W*"):
                state.clipped = True
            elif op in ("BDC", "BMC"):
                optional.append(bool(operands) and str(operands[0]) == "/OC")
            elif op == "EMC" and optional:
                optional.pop()
            elif op == "Tf" and operands:
                state.font = _dictionary(fonts.get(str(operands[0])))
            elif op == "Tr" and operands:
                state.render = _render_mode(operands[0])
            elif op == "gs" and operands:
                gstate = _dictionary(states.get(str(operands[0])))
                if gstate is None:
                    continue
                selected = gstate.get("/Font")
                if isinstance(selected, pikepdf.Array) and len(selected):
                    state.font = _dictionary(selected[0])
                if "/ca" in gstate:
                    alpha = _numbers([gstate.get("/ca")])
                    state.fill_alpha = alpha[0] if alpha else 0.0
                if "/BM" in gstate:
                    state.blend_normal = str(gstate.get("/BM")) in ("/Normal", "/Compatible")
                if "/SMask" in gstate:
                    state.soft_mask = gstate.get("/SMask") != pikepdf.Name("/None")
                if "/op" in gstate or "/OP" in gstate:
                    # /OP also sets nonstroking overprint when /op is absent.
                    state.overprint = gstate.get("/op", gstate.get("/OP")) is not False
            elif op == "Do" and operands:
                child = xobjects.get(str(operands[0]))
                if isinstance(child, pikepdf.Stream) and child.get("/Subtype") == "/Form":
                    self.form(child, resources, state, ancestors, coverable=coverable)
                elif (
                    box is not None
                    and not state.clipped
                    and state.opaque
                    and not any(optional)
                    and _opaque_image(child)
                    and _covers(state.ctm, box)
                ):
                    self.sequence += 1
                    self.last_cover = self.sequence
            elif op in TEXT_SHOWING and operands:
                self.show(state.font, state.render, op, operands, coverable=coverable)

    def page(self, page) -> None:
        self.page_shows, self.last_cover = [], -1
        box = _visible_box(page)
        self.walk(page, None, _State(), coverable=True, box=box)
        for sequence, key in self.page_shows:
            if sequence > self.last_cover:
                self.uses.visible.add(key)

    def appearance(self, obj, resources, ancestors=frozenset()) -> None:
        """Annotation and field appearances, painted over the page: never covered."""
        if isinstance(obj, pikepdf.Stream):
            self.walk(obj, resources, _State(), coverable=False)
            return
        if not isinstance(obj, pikepdf.Dictionary):
            return
        key = _obj_key(obj)
        if key in ancestors:
            return
        resources = obj.get("/Resources", resources)
        for _, child in obj.items():
            self.appearance(child, resources, ancestors | ({key} if key else set()))


def simple_font_uses(pdf) -> SimpleFontUses:
    walker = _Walker()
    for page in pdf.pages:
        walker.page(page)
    for kind, obj, resources in _iter_pdf_resource_roots(pdf, TOUNICODE_RESOURCES):
        if kind == "appearance":
            walker.appearance(obj, resources)
    acroform = _dictionary(pdf.Root.get("/AcroForm"))
    defaults = _dictionary(acroform.get("/DR")) if acroform is not None else None
    fonts = _dictionary(defaults.get("/Font")) if defaults is not None else None
    for _, font in (fonts or {}).items():
        if (key := _obj_key(font)) is not None:
            walker.uses.visible.add(key)
            walker.uses.unknown_codes.add(key)
    return walker.uses


def glyphless_program(font_dict, codes: set[int]) -> bytes | None:
    """An empty-glyph TrueType program for a nonsymbolic simple TrueType font.

    Each shown code's glyph name is in the Adobe Glyph List and names one
    character; the program's (3,1) cmap maps that character to an empty
    glyph whose advance is the code's PDF width. Returns None when a code has
    no such name, or two codes would need one glyph to have two advances.
    """
    from fontTools.agl import LEGACY_AGL2UV, UV2AGL
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    if font_dict.get("/Subtype") != "/TrueType":
        return None
    descriptor = _dictionary(font_dict.get("/FontDescriptor"))
    widths = font_dict.get("/Widths")
    if descriptor is None or not isinstance(widths, pikepdf.Array):
        return None
    try:
        if int(descriptor.get("/Flags", 0) or 0) & 4:
            return None
        first = int(font_dict.get("/FirstChar"))
        missing = float(descriptor.get("/MissingWidth", 0) or 0)
    except (TypeError, ValueError):
        return None

    advances: dict[int, int] = {}
    for code in sorted(codes):
        named = _glyph_name_for_code(font_dict, code, None)
        unicode = LEGACY_AGL2UV.get(named[0]) if named else None
        if unicode is None or len(unicode) != 1:
            return None
        try:
            width = float(widths[code - first]) if 0 <= code - first < len(widths) else missing
        except (TypeError, ValueError):
            return None
        advance = round(width)
        if not 0 <= advance <= 0xFFFF or advances.setdefault(unicode[0], advance) != advance:
            return None

    names = {uv: UV2AGL.get(uv) or f"uni{uv:04X}" for uv in advances}
    order = [".notdef", *names.values()]
    builder = FontBuilder(1000, isTTF=True)
    builder.updateHead(created=_EPOCH_2000, modified=_EPOCH_2000)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(names)
    builder.setupGlyf({name: TTGlyphPen(None).glyph() for name in order})
    notdef = round(missing) if 0 <= missing <= 0xFFFF else 0
    metrics = {".notdef": (notdef, 0)}
    metrics.update({names[uv]: (advance, 0) for uv, advance in advances.items()})
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=1000, descent=-250)
    builder.setupNameTable({"familyName": "GlyphLessFont", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=1000, sTypoDescender=-250, usWinAscent=1000, usWinDescent=250)
    builder.setupPost()
    buffer = BytesIO()
    builder.save(buffer)
    return buffer.getvalue()
