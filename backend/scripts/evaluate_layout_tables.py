"""Offline paired re-tagging and author-table checks for layout classification.

Reads captures and references without writing to them. All outputs go under
--output. No OCR, extraction, AI, font repair or full release gate is replayed.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pikepdf

from app.pipeline import tagger
from app.pipeline.fidelity import _output_tables_without_headers
from app.pipeline.page_glyphs import PdfGlyphReader, union_bbox
from app.pipeline.pdfium_text import read_page_text
from app.pipeline.tagger_layout_tables import _layout_table_plan
from app.services.structure_text import MarkedContentReader, _form_paths, _standard_role


def reference_tables(path: Path) -> list[dict]:
    """Recover author cells and their actual marked-content geometry."""
    with pikepdf.open(path) as pdf, PdfGlyphReader(path) as glyphs:
        root = pdf.Root.get('/StructTreeRoot', {})
        role_map = root.get('/RoleMap')
        pages = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
        reader = MarkedContentReader(pdf, glyphs, read_page_text(path))
        paths = {}
        page_glyphs = {}
        tables = []

        def content(node, page=None):
            if isinstance(node, pikepdf.Array):
                parts = [content(k, page) for k in node]
                return ' '.join(p[0] for p in parts), [g for p in parts for g in p[1]]
            stream = None
            if isinstance(node, pikepdf.Dictionary):
                if '/Pg' in node:
                    page = pages.get(node.Pg.objgen, page)
                if node.get('/Type') == '/MCR':
                    stream, mcid = node.get('/Stm'), int(node.MCID)
                else:
                    value, drawn = content(node.get('/K'), page)
                    return str(node.get('/ActualText', value)), drawn
            elif isinstance(node, int):
                mcid = node
            else:
                return '', []
            if page is None:
                return '', []
            value = reader.read(page, stream, mcid) or ''
            if page not in paths:
                paths[page] = _form_paths(pdf.pages[page].obj)
            stream_path = () if stream is None else paths[page].get(stream.objgen)
            if page not in page_glyphs:
                page_glyphs[page] = glyphs.page(page)
            drawn = [(page, g) for g in page_glyphs[page].marked.get((stream_path, mcid), [])
                     if not isinstance(g, str)]
            return value, drawn

        def children(node, role):
            if isinstance(node, pikepdf.Array):
                for kid in node:
                    yield from children(kid, role)
            elif isinstance(node, pikepdf.Dictionary):
                if _standard_role(node, role_map) == role:
                    yield node
                else:
                    yield from children(node.get('/K'), role)

        def walk(node, page=None):
            if isinstance(node, pikepdf.Array):
                for kid in node:
                    walk(kid, page)
            elif isinstance(node, pikepdf.Dictionary):
                if '/Pg' in node:
                    page = pages.get(node.Pg.objgen, page)
                if _standard_role(node, role_map) == 'Table':
                    cells, all_drawn = [], []
                    trs = list(children(node.get('/K'), 'TR'))
                    for r, row in enumerate(trs):
                        col = 0
                        # TH/TD may be wrapped in nonstandard grouping tags.
                        def row_cells(k):
                            if isinstance(k, pikepdf.Array):
                                for child in k:
                                    yield from row_cells(child)
                            elif isinstance(k, pikepdf.Dictionary):
                                if _standard_role(k, role_map) in {'TH', 'TD'}:
                                    yield k
                                else:
                                    yield from row_cells(k.get('/K'))
                        for cell in row_cells(row.get('/K')):
                            reader.last_read = None
                            value, drawn = content(cell, page)
                            all_drawn.extend(drawn)
                            attrs = cell.get('/A', {})
                            if isinstance(attrs, pikepdf.Array):
                                attrs = next((a for a in attrs if a.get('/O') == '/Table'), {})
                            cells.append(dict(row=r, col=col, text=value,
                                              is_header=_standard_role(cell, role_map) == 'TH',
                                              bbox=union_bbox(g.bbox for _, g in drawn) if drawn else None,
                                              row_span=int(attrs.get('/RowSpan', 1)),
                                              col_span=int(attrs.get('/ColSpan', 1))))
                            col += int(attrs.get('/ColSpan', 1))
                    boxes = {p: union_bbox(g.bbox for pg, g in all_drawn if pg == p)
                             for p in {pg for pg, _ in all_drawn}}
                    tables.append(dict(type='table', cells=cells, num_rows=len(trs),
                                       num_cols=max((c['col'] + c['col_span'] for c in cells), default=0),
                                       pages=boxes, text=' '.join(c['text'] for c in cells)))
                walk(node.get('/K'), page)
        walk(root.get('/K'))
        return tables


def marked_signature(path: Path):
    with PdfGlyphReader(path) as reader, pikepdf.open(path) as pdf:
        return [{key: [(g if isinstance(g, str) else (g.text, g.bbox)) for g in value]
                 for key, value in reader.page(p).marked.items()}
                for p in range(len(pdf.pages))]


def evaluate(job):
    capture, output = map(Path, job)
    output.mkdir(parents=True, exist_ok=True)
    structure = json.loads((capture / 'structure.json').read_text())
    arguments = json.loads((capture / 'arguments.json').read_text())
    original = copy.deepcopy(structure)
    plans = []
    n = 0
    for t in structure['elements']:
        if t.get('type') != 'table' or not t.get('num_rows') or not t.get('num_cols'):
            continue
        n += 1
        plan = _layout_table_plan(t, [e for e in structure['elements'] if e['page'] == t['page']])
        if plan:
            plans.append(dict(index=n, page=t['page'] + 1, reason=plan['reason'], order=plan['order'],
                              text=' '.join(c.get('text', '') for c in t['cells']), bbox=t['bbox']))
    classifier = tagger._layout_table_plan
    try:
        for name in ('before', 'after'):
            tagger._layout_table_plan = (lambda *args: None) if name == 'before' else classifier
            asyncio.run(tagger.tag_pdf(capture / 'input.pdf', output / f'{name}.pdf', structure, **arguments))
    finally:
        tagger._layout_table_plan = classifier
    assert structure == original
    # Compare the precise text/geometry at every (page, form path, MCID).
    assert marked_signature(output / 'before.pdf') == marked_signature(output / 'after.pdf')
    missing = {name: _output_tables_without_headers(output / f'{name}.pdf') for name in ('before', 'after')}
    run = json.loads(capture.with_name(capture.name.replace('.tag-inputs', '.run.json')).read_text())
    result = dict(document=f'{capture.parent.name}/{capture.name.removesuffix(".tag-inputs")}',
                demoted=plans, missing=missing, outcome=run['outcome'])
    (output / 'result.json').write_text(json.dumps(result, indent=1))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--captures', type=Path, required=True)
    parser.add_argument('--references', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--references-only', action='store_true', help='reuse completed paired outputs')
    options = parser.parse_args()
    options.output.mkdir(parents=True, exist_ok=True)
    jobs = [(str(c), str(options.output / c.parent.name / c.name.removesuffix('.tag-inputs')))
            for c in sorted(options.captures.glob('*/*.tag-inputs'))]
    if options.references_only:
        results = [json.loads((Path(o) / "result.json").read_text()) for _, o in jobs]
    else:
        with ProcessPoolExecutor(options.workers) as pool:
            results = list(pool.map(evaluate, jobs))
    refs = []
    for subset in ('gold_rt', 'cuny_rt'):
        for path in sorted((options.references / subset).glob('*.pdf')):
            tables = reference_tables(path)
            # A stronger counterfactual also removes all author header flags.
            # This does not replace matching candidate demotions below.
            counterfactual = []
            for index, table in enumerate(tables, 1):
                no_headers = copy.deepcopy(table)
                for cell in no_headers['cells']:
                    cell['is_header'] = False
                if plan := _layout_table_plan(no_headers, []):
                    counterfactual.append(dict(index=index, reason=plan['reason']))
            refs.append(dict(document=f'{subset}/{path.name}', tables=tables,
                             counterfactual=counterfactual))
    gold_names = {
        'FishTables-only': 'fish_tables', 'SpringMT2023': 'noaa_spring_mt_2023',
        'pdfa-matterhorn-protocol-1.1': 'matterhorn_protocol',
        'moore-pdfua-paper-tagged17-18Sept': 'moore_pdfua_paper',
        'PDFUA-Ref-2-01_Magazine-danish': 'ref_magazine_da',
        'PDFUA-Ref-2-02_Invoice': 'ref_invoice',
        'PDFUA-Ref-2-03_AcademicAbstract': 'ref_academic_abstract',
        'PDFUA-Ref-2-04_Presentation': 'ref_presentation',
        'PDFUA-Ref-2-05_BookChapter-german': 'ref_book_chapter_de',
        'PDFUA-Ref-2-06_Brochure': 'ref_brochure',
        'PDFUA-Ref-2-08_BookChapter': 'ref_book_chapter',
    }
    overlaps = []
    for ref in refs:
        subset, filename = ref['document'].split('/')
        stem = Path(filename).stem
        document = f"{subset}/{gold_names.get(stem, stem)}"
        result = next((r for r in results if r['document'] == document), None)
        if result is None:
            raise ValueError(f'No capture mapped to {ref["document"]}')
        for index, table in enumerate(ref['tables'], 1):
            for demoted in result['demoted']:
                box = table['pages'].get(demoted['page'] - 1)
                if box is None:
                    continue
                b = demoted['bbox']
                intersection = max(0, min(box['r'], b['r']) - max(box['l'], b['l'])) * max(0, min(box['t'], b['t']) - max(box['b'], b['b']))
                area = max(1, (box['r'] - box['l']) * (box['t'] - box['b']))
                if intersection > 0:
                    overlaps.append(dict(document=document, author_table=index,
                                         demoted=demoted['index'], author_area_share=intersection / area,
                                         author_text=table['text']))
    summary = dict(documents=len(results), demoted=sum(len(r['demoted']) for r in results),
                   held_before=sum(bool(r['missing']['before']) for r in results),
                   held_after=sum(bool(r['missing']['after']) for r in results),
                   tables_held_before=sum(len(r['missing']['before']) for r in results),
                   tables_held_after=sum(len(r['missing']['after']) for r in results),
                   reference_documents=len(refs), reference_tables=sum(len(r['tables']) for r in refs),
                   counterfactual_demotions=sum(len(r['counterfactual']) for r in refs),
                   author_region_overlaps=len(overlaps),
                   reasons=dict(Counter(d['reason'] for r in results for d in r['demoted'])))
    (options.output / 'measurement.json').write_text(json.dumps(dict(summary=summary, results=results, references=refs, author_overlaps=overlaps), indent=1))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
