"""Rendered ToUnicode CMaps read back exactly and keep bfrange within its limits."""

import random

import pikepdf
import pypdfium2
import pytest

from app.pipeline.fonts.encoding import (
    HEX_STR_RE,
    _is_valid_unicode_text,
    _parse_tounicode_map,
    _render_tounicode_cmap,
)
from tests.test_heldout_release_repairs import _cid_pdf


def _valid(mapping):
    return {code: text for code, text in mapping.items() if _is_valid_unicode_text(text)}


def _blocks(data: bytes, code_bytes: int):
    """Each block's kind and entries, checking the counts and the CMap limits."""
    lines = data.decode("ascii").splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        words = lines[index].split()
        if len(words) == 2 and words[1] in ("beginbfchar", "beginbfrange"):
            kind, count = words[1][5:], int(words[0])
            entries = []
            index += 1
            while lines[index] != f"end{kind}":
                entries.append(HEX_STR_RE.findall(lines[index]))
                index += 1
            assert 0 < count == len(entries) <= 100
            for entry in entries:
                assert all(len(code) == 2 * code_bytes for code in entry[:-1])
                if kind == "bfrange":
                    low, high = int(entry[0], 16), int(entry[1], 16)
                    destination = bytes.fromhex(entry[2])
                    # Only the last source byte varies, the last destination
                    # byte is not carried past 255, and the destination is
                    # one UTF-16 code unit.
                    assert low < high and low >> 8 == high >> 8
                    assert len(destination) == 2
                    assert destination[-1] + (high - low) <= 0xFF
            blocks.append((kind, entries))
        index += 1
    return blocks


def _random_map(seed: int, code_bytes: int) -> dict[int, str]:
    rng = random.Random(seed)
    top = (1 << (8 * code_bytes)) - 1
    mapping: dict[int, str] = {}
    code = rng.randrange(0, 64)
    while code <= top and len(mapping) < 3000:
        kind = rng.random()
        length = rng.randrange(1, 40 if code_bytes == 1 else 300)
        if kind < 0.6:
            start = rng.choice([0x20, 0xF0, 0x3B0, 0x4DF0, 0xFEF0, 0xFFE0, 0x1F580])
            start += rng.randrange(0, 40)
            for offset in range(length):
                mapping[code + offset] = chr(min(start + offset, 0x10FFFF))
        elif kind < 0.8:
            for offset in range(length):
                mapping[code + offset] = chr(rng.randrange(0x20, 0x2FFFF))
        else:
            for offset in range(length):
                mapping[code + offset] = "".join(
                    chr(rng.randrange(0x20, 0x3000)) for _ in range(rng.randrange(1, 4))
                )
        code += length + rng.choice([0, 0, 1, 2, 50])
    return _valid({key: value for key, value in mapping.items() if key <= top})


STRUCTURED = {
    "identity": (2, {code: chr(code) for code in range(0x20, 0x2000)}),
    "source-crosses-256": (2, {0xF0 + i: chr(0x4E00 + i) for i in range(0x40)}),
    "destination-crosses-256": (2, {0x110 + i: chr(0xF0 + i) for i in range(0x40)}),
    "gaps": (2, {code: chr(code) for code in range(0x20, 0x400) if code % 7}),
    "source-gaps-consecutive-destinations": (
        2,
        {0x41: "A", 0x43: "B", 0x44: "C", 0x46: "D", 0x1FF: "E", 0x200: "F"},
    ),
    "decreasing-destinations": (2, {0x41 + i: chr(0x5A - i) for i in range(26)}),
    "multi-codepoint": (
        2,
        {0x10: "ffi", 0x11: "fi", 0x12: "fl", 0x13: "A", 0x14: "B", 0x15: "A\u0301"},
    ),
    "supplementary": (2, {0x20 + i: chr(0x1F580 + i) for i in range(0x20)}),
    "around-excluded-values": (2, {0x10: "\ufefd", 0x11: "\ufefe", 0x12: "\uff00", 0x13: "\uff01"}),
    "single-byte": (1, {code: chr(code) for code in range(0x20, 0x100) if code != 0x7F}),
    "single-byte-greek": (1, {0x41 + i: chr(0x391 + i) for i in range(17)}),
    "hidden-ocr-identity": (2, _valid({code: chr(code) for code in range(1, 0x10000)})),
}


@pytest.mark.parametrize("name", list(STRUCTURED))
def test_structured_maps_read_back_exactly(name):
    code_bytes, mapping = STRUCTURED[name]
    mapping = _valid(mapping)
    data = _render_tounicode_cmap(mapping, code_bytes)
    _blocks(data, code_bytes)
    assert _parse_tounicode_map(pikepdf.Stream(pikepdf.new(), data)) == mapping


@pytest.mark.parametrize("code_bytes", [1, 2])
@pytest.mark.parametrize("seed", range(12))
def test_random_maps_read_back_exactly(seed, code_bytes):
    mapping = _random_map(seed, code_bytes)
    data = _render_tounicode_cmap(mapping, code_bytes)
    _blocks(data, code_bytes)
    assert _parse_tounicode_map(pikepdf.Stream(pikepdf.new(), data)) == mapping


def test_consecutive_runs_become_ranges():
    _, mapping = STRUCTURED["hidden-ocr-identity"]
    blocks = _blocks(_render_tounicode_cmap(mapping, 2), 2)
    assert sum(len(entries) for _, entries in blocks) < 300
    _, gaps = STRUCTURED["gaps"]
    kinds = {kind for kind, _ in _blocks(_render_tounicode_cmap(_valid(gaps), 2), 2)}
    assert kinds == {"bfrange", "bfchar"}


def test_pdfium_extracts_the_rendered_ranges(tmp_path):
    path = tmp_path / "ranges.pdf"
    mapping = {
        **{0xFE + i: chr(0x41 + i) for i in range(4)},
        **{0x200 + i: chr(0xFE + i) for i in range(4)},
        0x300: "ffi",
        0x301: "\U0001f589",
    }
    codes = [*mapping]
    _cid_pdf(path, "A")
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        font = pdf.pages[0].Resources.Font.F1
        font.ToUnicode = pdf.make_stream(_render_tounicode_cmap(mapping, 2))
        shown = "".join(f"{code:04X}" for code in codes)
        pdf.pages[0].Contents = pdf.make_stream(f"BT /F1 12 Tf 50 50 Td <{shown}> Tj ET".encode())
        pdf.save(path)
    document = pypdfium2.PdfDocument(path)
    try:
        assert document[0].get_textpage().get_text_range() == "".join(mapping.values())
    finally:
        document.close()
