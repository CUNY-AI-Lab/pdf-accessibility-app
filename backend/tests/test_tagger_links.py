"""A link inside a paragraph stays reachable: nothing above it in the
structure tree carries /ActualText, which would replace the link for a
screen reader."""

import pikepdf
import pytest

from app.pipeline.tagger import tag_pdf
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import helvetica

LINES = ["The first line of the paragraph", "see example.org for more", "and the last line here"]


@pytest.mark.asyncio
async def test_a_link_in_a_paragraph_is_not_hidden_by_actual_text(tmp_path):
    source = tmp_path / "link.pdf"
    tagged = tmp_path / "tagged.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    page.Contents = pdf.make_stream(
        " ".join(
            f"BT /F1 10 Tf 72 {700 - 12 * index} Td ({line}) Tj ET"
            for index, line in enumerate(LINES)
        ).encode()
    )
    link = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name.Link,
            Rect=[88, 686, 150, 697],
            A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String("https://example.org")),
        )
    )
    page.Annots = pdf.make_indirect(pikepdf.Array([link]))
    pdf.save(source)
    text = " ".join(LINES)

    await tag_pdf(
        input_path=source,
        output_path=tagged,
        structure_json={
            "title": "Links",
            "elements": [
                {
                    "type": "paragraph",
                    "page": 0,
                    "text": text,
                    "bbox": {"l": 70, "b": 673, "r": 250, "t": 712},
                },
            ],
        },
        alt_texts=[],
        language="en",
        original_filename=source.name,
    )

    with pikepdf.open(tagged) as result:

        def link_ancestors(node, ancestors):
            if isinstance(node, pikepdf.Array):
                for kid in node:
                    yield from link_ancestors(kid, ancestors)
            elif isinstance(node, pikepdf.Dictionary) and "/S" in node:
                if node.S == "/Link":
                    yield ancestors
                elif "/K" in node:
                    yield from link_ancestors(node.K, [*ancestors, node])

        found = list(link_ancestors(result.Root.StructTreeRoot.K, []))
        assert len(found) == 1
        assert not any("/ActualText" in ancestor for ancestor in found[0])
    assert " ".join(screen_reader_text(tagged).split()) == text
