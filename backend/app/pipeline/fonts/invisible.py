"""Give fonts that only show invisible text an empty glyph for every code.

Acrobat's hidden OCR layer (``HiddenHorzOCR``) shows recognized text in text
rendering mode 3 with a CFF program whose only glyph is .notdef. PDF/UA
7.21.8 rejects .notdef references in every rendering mode, and deleting the
codes would delete the recognized text that ToUnicode exposes. Tesseract's
``GlyphLessFont`` (``src/api/pdfrenderer.cpp``), also used by OCRmyPDF, shows
the same kind of layer with a CIDFontType2 whose ``/CIDToGIDMap`` sends every
CID to glyph 1 of a tiny TrueType program. This pass gives qualifying fonts
that descendant and keeps the Type0 dictionary, ToUnicode and widths.
"""

from io import BytesIO

import pikepdf

from app.pipeline.fonts.resources import TOUNICODE_RESOURCES, _iter_pdf_resource_roots, _obj_key

TEXT_SHOWING = frozenset({"Tj", "TJ", "'", '"'})
INVISIBLE = 3
# 2000-01-01 in TrueType seconds since 1904: a fixed stamp keeps output stable.
_EPOCH_2000 = 3_029_529_600


def _render_mode(value) -> int:
    """An unreadable mode counts as painted."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -1
    return int(number) if number.is_integer() else -1


def _dictionary(obj):
    return obj if isinstance(obj, pikepdf.Dictionary) else None


class _FontUses:
    """Codes shown by each indirect Type0 font, and the fonts ever painted.

    Font and rendering mode are graphics state: q/Q restore them, BT/ET do
    not reset them, and a Form XObject starts from its invoker's state.
    Streams whose initial state is unknown (patterns, Type 3 glyphs, soft
    masks, appearances) start from the defaults, rendering mode 0.
    """

    def __init__(self):
        self.fonts = {}
        self.codes = {}
        self.painted = set()
        self._walked = set()
        self._extras = set()

    def show(self, font, render, op, operands) -> None:
        key = _obj_key(font)
        if key is None or font.get("/Subtype") != pikepdf.Name("/Type0") or not operands:
            return
        self.fonts[key] = font
        if render != INVISIBLE:
            self.painted.add(key)
        values = operands[0] if op == "TJ" else [operands[-1]]
        if not isinstance(values, (pikepdf.Array, list)):
            return
        codes = self.codes.setdefault(key, set())
        for value in values:
            if isinstance(value, pikepdf.String):
                raw = bytes(value)
                codes.update(
                    int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw) - 1, 2)
                )

    def walk_extras(self, resources, ancestors) -> None:
        """Content streams reachable through resources rather than Do."""
        key = _obj_key(resources)
        if key is not None:
            if key in self._extras:
                return
            self._extras.add(key)
        for _, pattern in (_dictionary(resources.get("/Pattern")) or {}).items():
            if isinstance(pattern, pikepdf.Stream) and pattern.get("/PatternType") == 1:
                self.walk(pattern, resources, None, 0, ancestors)
        for _, font in (_dictionary(resources.get("/Font")) or {}).items():
            if isinstance(font, pikepdf.Dictionary) and font.get("/Subtype") == "/Type3":
                own = _dictionary(font.get("/Resources")) or resources
                for _, proc in (_dictionary(font.get("/CharProcs")) or {}).items():
                    if isinstance(proc, pikepdf.Stream):
                        self.walk(proc, own, None, 0, ancestors)
        for _, state in (_dictionary(resources.get("/ExtGState")) or {}).items():
            mask = (
                _dictionary(state.get("/SMask")) if isinstance(state, pikepdf.Dictionary) else None
            )
            group = mask.get("/G") if mask is not None else None
            if isinstance(group, pikepdf.Stream):
                self.walk(group, resources, None, 0, ancestors)

    def walk(self, owner, inherited, font, render, ancestors=frozenset()) -> None:
        """Walk one content stream; it uses its own resources if it has them."""
        obj = owner.obj if isinstance(owner, pikepdf.Page) else owner
        key = obj.objgen
        if key in ancestors:
            return
        resources = _dictionary(obj.get("/Resources"))
        if resources is not None:
            scope = "own"
        else:
            resources = _dictionary(inherited) or pikepdf.Dictionary()
            # A direct inherited dictionary has no identity to memoize on.
            scope = _obj_key(resources) or object()
        visit = (key, _obj_key(font), render, scope)
        if visit in self._walked:
            return
        self._walked.add(visit)
        ancestors = ancestors | {key}
        self.walk_extras(resources, ancestors)
        try:
            instructions = pikepdf.parse_content_stream(owner)
        except (pikepdf.PdfError, ValueError, TypeError):
            return
        fonts = _dictionary(resources.get("/Font")) or {}
        stack = []
        for instruction in instructions:
            op, operands = str(instruction.operator), list(instruction.operands)
            if op == "q":
                stack.append((font, render))
            elif op == "Q" and stack:
                font, render = stack.pop()
            elif op == "Tf" and operands:
                font = _dictionary(fonts.get(str(operands[0])))
            elif op == "Tr" and operands:
                render = _render_mode(operands[0])
            elif op == "gs" and operands:
                states = _dictionary(resources.get("/ExtGState")) or {}
                state = _dictionary(states.get(str(operands[0])))
                selected = state.get("/Font") if state is not None else None
                if isinstance(selected, pikepdf.Array) and len(selected):
                    font = _dictionary(selected[0])
            elif op == "Do" and operands:
                xobjects = _dictionary(resources.get("/XObject")) or {}
                child = xobjects.get(str(operands[0]))
                if isinstance(child, pikepdf.Stream) and child.get("/Subtype") == "/Form":
                    self.walk(child, resources, font, render, ancestors)
            elif op in TEXT_SHOWING and font is not None:
                self.show(font, render, op, operands)

    def appearance(self, obj, resources, ancestors=frozenset()) -> None:
        """Annotation and field appearances: /AP, its N/R/D children and states."""
        if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
            return
        if isinstance(obj, pikepdf.Stream):
            self.walk(obj, resources, None, 0)
            return
        key = _obj_key(obj)
        if key in ancestors:
            return
        resources = obj.get("/Resources", resources)
        for _, child in obj.items():
            self.appearance(child, resources, ancestors | ({key} if key else set()))

    @classmethod
    def collect(cls, pdf) -> "_FontUses":
        uses = cls()
        for page in pdf.pages:
            uses.walk(page, None, None, 0)
        for kind, obj, resources in _iter_pdf_resource_roots(pdf, TOUNICODE_RESOURCES):
            if kind == "appearance":
                uses.appearance(obj, resources)
        # A viewer may regenerate field appearances with any default-resource
        # font, and nothing says how it would render them.
        acroform = _dictionary(pdf.Root.get("/AcroForm"))
        defaults = _dictionary(acroform.get("/DR")) if acroform is not None else None
        fonts = _dictionary(defaults.get("/Font")) if defaults is not None else None
        for _, font in (fonts or {}).items():
            key = _obj_key(font)
            if key is not None:
                uses.painted.add(key)
        return uses


def _missing_glyph_test(cid_font):
    """Return a predicate for CIDs that select .notdef, or None if unknown."""
    from fontTools.cffLib import CFFFontSet
    from fontTools.ttLib import TTFont

    descriptor = _dictionary(cid_font.get("/FontDescriptor"))
    if descriptor is None:
        return None
    subtype = cid_font.get("/Subtype")
    if subtype == "/CIDFontType0":
        program = descriptor.get("/FontFile3")
        if not isinstance(program, pikepdf.Stream):
            return None
        data = BytesIO(program.read_bytes())
        if program.get("/Subtype") == "/OpenType":
            with TTFont(data) as font:
                cff = font["CFF "].cff
        elif program.get("/Subtype") in ("/CIDFontType0C", "/Type1C"):
            cff = CFFFontSet()
            cff.decompile(data, None)
        else:
            return None
        top = cff[cff.fontNames[0]]
        charset = list(top.charset)
        if getattr(top, "ROS", None):
            present = {
                int(name[3:]) for name in charset if name.startswith("cid") and name[3:].isdigit()
            }
            return lambda cid: cid not in present
        # A CFF program that is not CID-keyed is indexed by CID directly.
        return lambda cid: cid >= len(charset)
    if subtype == "/CIDFontType2":
        program = descriptor.get("/FontFile2")
        if not isinstance(program, pikepdf.Stream):
            return None
        with TTFont(BytesIO(program.read_bytes())) as font:
            count = font["maxp"].numGlyphs
        cidmap = cid_font.get("/CIDToGIDMap", pikepdf.Name.Identity)
        if cidmap == pikepdf.Name.Identity:
            return lambda cid: cid >= count
        if not isinstance(cidmap, pikepdf.Stream):
            return None
        raw = cidmap.read_bytes()

        def missing(cid):
            gid = (
                int.from_bytes(raw[2 * cid : 2 * cid + 2], "big") if 2 * cid + 2 <= len(raw) else 0
            )
            return gid == 0 or gid >= count

        return missing
    return None


def _shows_notdef(font, codes) -> bool:
    """A nonzero used code reaches .notdef; CID 0 is .notdef in every font."""
    if font.get("/Encoding") != pikepdf.Name("/Identity-H"):
        return False
    descendants = font.get("/DescendantFonts")
    if not isinstance(descendants, pikepdf.Array) or len(descendants) != 1:
        return False
    cid_font = _dictionary(descendants[0])
    if cid_font is None:
        return False
    try:
        missing = _missing_glyph_test(cid_font)
    except Exception:
        return False
    return missing is not None and any(missing(code) for code in codes if code)


def _glyphless_program(advance: int) -> bytes:
    """Glyph 0 .notdef and glyph 1, both empty, with one advance.

    veraPDF's 7.21.5 width check exempts rendering mode 3, so one advance
    serves every CID; it is the descendant's /DW.
    """
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    order = [".notdef", "blank"]
    builder = FontBuilder(1000, isTTF=True)
    builder.updateHead(created=_EPOCH_2000, modified=_EPOCH_2000)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({})
    builder.setupGlyf({name: TTGlyphPen(None).glyph() for name in order})
    builder.setupHorizontalMetrics({name: (advance, 0) for name in order})
    builder.setupHorizontalHeader(ascent=1000, descent=-250)
    builder.setupNameTable({"familyName": "GlyphLessFont", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=1000, sTypoDescender=-250, usWinAscent=1000, usWinDescent=250)
    builder.setupPost()
    buffer = BytesIO()
    builder.save(buffer)
    return buffer.getvalue()


def repair_invisible_fonts(pdf) -> dict[str, int]:
    """Replace the descendant of each invisible-only font that shows .notdef.

    The Type0 dictionary, /Encoding and /ToUnicode stay; the descendant keeps
    /BaseFont, /CIDSystemInfo, /W and /DW. A font painted anywhere, or listed
    in the AcroForm default resources, is never replaced. Program and map
    streams are shared by every font that needs the same bytes.
    """
    try:
        uses = _FontUses.collect(pdf)
    except Exception:
        # Collection only reads. A malformed document must not stop the
        # other dictionary repairs; its validation failure stays reported.
        return {"invisible_fonts_replaced": 0}
    programs, maps = {}, {}
    replaced = 0
    for key, font in uses.fonts.items():
        codes = uses.codes.get(key, set())
        if key in uses.painted or not _shows_notdef(font, codes):
            continue
        cid_font = font.DescendantFonts[0]
        try:
            advance = round(float(cid_font.get("/DW", 1000)))
        except (TypeError, ValueError):
            continue
        advance = min(max(advance, 0), 0xFFFF)
        if advance not in programs:
            data = _glyphless_program(advance)
            programs[advance] = pdf.make_stream(data, Length1=len(data))
        top = max(codes)
        if top not in maps:
            # CID 0 stays glyph 0: validators treat it as .notdef regardless.
            maps[top] = pdf.make_stream(b"\0\0" + b"\0\1" * top)
        descriptor = pikepdf.Dictionary(cid_font.FontDescriptor)
        for name in ("/FontFile", "/FontFile2", "/FontFile3", "/CIDSet"):
            if name in descriptor:
                del descriptor[name]
        descriptor.FontFile2 = programs[advance]
        descendant = pikepdf.Dictionary(cid_font)
        descendant.Subtype = pikepdf.Name.CIDFontType2
        descendant.FontDescriptor = pdf.make_indirect(descriptor)
        descendant.CIDToGIDMap = maps[top]
        font.DescendantFonts = pikepdf.Array([pdf.make_indirect(descendant)])
        replaced += 1
    return {"invisible_fonts_replaced": replaced}
