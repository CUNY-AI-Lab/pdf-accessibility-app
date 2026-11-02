"""Typed reads of pikepdf's dynamically decoded PDF values."""

import unicodedata

import pikepdf


def pdf_dictionary(value: object) -> pikepdf.Dictionary | None:
    return value if isinstance(value, pikepdf.Dictionary) else None


def pdf_name(value: object) -> str | None:
    """Return a resource/role key only when the PDF value is a Name."""
    return str(value) if isinstance(value, pikepdf.Name) else None


def pdf_named_resource(resources: object, name: object) -> pikepdf.Object | None:
    """Look up a content-stream resource operand in a PDF dictionary."""
    mapping = pdf_dictionary(resources)
    key = pdf_name(name)
    if mapping is None or key is None:
        return None
    value = mapping.get(key)
    return value if isinstance(value, pikepdf.Object) else None


def pdf_text(value: object, default: str = "") -> str:
    """Decode a PDF string or name, preserving the caller's missing default."""
    return str(value) if isinstance(value, (pikepdf.String, pikepdf.Name, str)) else default


def author_text(value: object) -> str:
    """Decode text an author put in a PDF, without its control characters.

    Word and other tools leave a trailing NUL, a vertical tab or carriage
    return in alt text, link contents and bookmark titles, which a screen
    reader announces or stops at. Line and tab breaks read as spaces, other
    control characters (Unicode Cc) are dropped, and spaces collapse.
    """
    text = "".join(
        character
        for character in pdf_text(value)
        if character.isspace() or unicodedata.category(character) != "Cc"
    )
    return " ".join(text.split())


def pdf_integer(value: object, default: int = 1) -> int:
    # pikepdf decodes PDF integers as Python ints.
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def pdf_page_key(value: object) -> tuple[int, int] | None:
    """Read the object identity of a /Pg dictionary reference."""
    page = pdf_dictionary(value)
    return page.objgen if page is not None else None
