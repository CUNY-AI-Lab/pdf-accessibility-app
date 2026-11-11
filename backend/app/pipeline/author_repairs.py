"""Structure-preserving candidate preparation for author-tagged PDFs."""

import copy
from pathlib import Path

import pikepdf

from app.pipeline.fonts.embedding import _embed_system_fonts_sync
from app.pipeline.fonts.repair import _repair_pdf_font_dicts_sync, _sync_pdf_cid_cff_widths_sync
from app.pipeline.fonts.tounicode import _repair_pdf_tounicode_sync
from app.pipeline.page_frame import PageFrame
from app.pipeline.page_glyphs import PdfGlyphReader, union_bbox
from app.pipeline.pdfium_text import PdfiumText
from app.pipeline.tagger_table_readability import _mark_unreadable_tables


def author_table_readability(path: Path, evidence: dict) -> list[dict]:
    """Run the same measured-glyph readability check on the author's cells."""
    tables = copy.deepcopy([e for e in evidence['elements'] if e['type'] == 'table'])
    diagnostics = []
    with PdfiumText(path) as visual, PdfGlyphReader(path) as reader:
        for page, pdf_page in enumerate(visual.document):
            frame = PageFrame.of(pdf_page)
            elements = []
            for table in tables:
                cells = [{**cell, 'bbox': frame.to_user_space(part['bbox'])}
                         for cell in table['cells'] for part in cell.get('page_parts', [cell])
                         if part.get('page') == page and part.get('bbox')]
                if cells:
                    elements.append({**table, 'page': page, 'cells': cells,
                                     'bbox': union_bbox(c['bbox'] for c in cells)})
            glyphs = [g for op in reader.page(page).ops.values() for run in op for g in run.glyphs]
            diagnostics.extend(_mark_unreadable_tables(elements, glyphs))
    return diagnostics


def repair_author_pdf(input_path: Path, output_path: Path, evidence: dict) -> list[dict]:
    """Only dictionary/font repairs; never OCR, Ghostscript or tag rebuilding.

    The caller must re-evaluate the resulting bytes and fall back to the
    passing original if any release check fails.
    """
    current = input_path
    audit = []
    for name, repair in (
        ('embed_fonts', _embed_system_fonts_sync),
        ('font_dicts', _repair_pdf_font_dicts_sync),
        ('tounicode', _repair_pdf_tounicode_sync),
        ('widths', _sync_pdf_cid_cff_widths_sync),
    ):
        target = output_path.with_name(f'author-{name}.pdf')
        changed, message, stats = repair(current, target)
        audit.append({'repair': name, 'applied': changed, 'message': message, 'stats': stats})
        if changed:
            current = target
    with pikepdf.open(current) as pdf:
        if evidence.get('language') and not pdf.Root.get('/Lang'):
            pdf.Root['/Lang'] = pikepdf.String(evidence['language'])
        if evidence.get('title'):
            pdf.docinfo['/Title'] = pikepdf.String(evidence['title'])
            with pdf.open_metadata() as metadata:
                metadata['dc:title'] = evidence['title']
        pdf.save(output_path)
    return audit
