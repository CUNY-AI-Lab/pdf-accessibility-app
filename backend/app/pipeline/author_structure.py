"""Read author semantics before re-tagging; transfer only unambiguous matches.

Evidence boxes are in Docling's displayed page frame. MCIDs are resolved in
their owning page/Form stream, never by document-wide MCID or page order.
"""

from __future__ import annotations

import copy
import json
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pikepdf

from app.pipeline.language import normalize_lang_tag
from app.pipeline.page_frame import PageFrame
from app.pipeline.page_glyphs import PdfGlyphReader, union_bbox
from app.pipeline.pdfium_text import PdfiumText, read_page_text
from app.pipeline.tagger_semantics import is_placeholder_alt_text
from app.services.structure_text import MarkedContentReader, _form_paths, _standard_role


def read_author_structure(path: Path) -> dict:
    """Recover cells, figures and headings with marked-content text/geometry."""
    with pikepdf.open(path) as pdf:
        root = pdf.Root.get('/StructTreeRoot')
        if not isinstance(root, pikepdf.Dictionary):
            return {}
        evidence = {'elements': [], 'language': normalize_lang_tag(str(pdf.Root.get('/Lang') or '')),
                    'title': str(pdf.docinfo.get('/Title') or '')}
        if not evidence['title']:
            with pdf.open_metadata() as metadata:
                evidence['title'] = str(metadata.get('dc:title', ''))
        pages = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
        role_map = root.get('/RoleMap')
        with PdfGlyphReader(path) as glyphs, PdfiumText(path) as visual:
            reader = MarkedContentReader(pdf, glyphs, read_page_text(path))
            paths = {}
            painted = {}
            frames = [PageFrame.of(p) for p in visual.document]

            def content(node, page=None, active=frozenset()):
                if isinstance(node, pikepdf.Array):
                    parts = [content(k, page, active) for k in node]
                    return ' '.join(p[0] for p in parts), [b for p in parts for b in p[1]]
                stream = None
                if isinstance(node, pikepdf.Dictionary):
                    key = node.objgen
                    if key != (0, 0) and key in active:
                        raise ValueError('Cyclic author structure')
                    active = active | {key} if key != (0, 0) else active
                    if '/Pg' in node:
                        page = pages.get(node.Pg.objgen, page)
                    if node.get('/Type') == '/MCR':
                        stream, mcid = node.get('/Stm'), int(node.MCID)
                    else:
                        value, boxes = content(node.get('/K'), page, active)
                        return str(node.get('/ActualText', value)), boxes
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
                boxes = [g.bbox for g in glyphs.page(page).marked.get((stream_path, mcid), [])
                         if not isinstance(g, str)]
                if not boxes:
                    if page not in painted:
                        painted[page] = visual.marked_bounds(page, pdf.pages[page].obj)
                    boxes = [dict(zip(('l', 'b', 'r', 't'), b, strict=True))
                             for b in painted[page].get((stream_path, mcid), [])]
                return value, [(page, b) for b in boxes]

            def attrs(node):
                value = node.get('/A', {})
                if isinstance(value, pikepdf.Array):
                    return next((a for a in value if isinstance(a, pikepdf.Dictionary)
                                 and a.get('/O') == '/Table'), {})
                return value

            def walk(node, page=None, table=None, row=-1, active=frozenset()):
                if isinstance(node, pikepdf.Array):
                    for kid in node:
                        walk(kid, page, table, row, active)
                elif isinstance(node, pikepdf.Dictionary):
                    key = node.objgen
                    if key != (0, 0) and key in active:
                        raise ValueError('Cyclic author structure')
                    active = active | {key} if key != (0, 0) else active
                    if '/Pg' in node:
                        page = pages.get(node.Pg.objgen, page)
                    role = _standard_role(node, role_map)
                    if role == 'Table':
                        table = {'type': 'table', 'cells': [], 'num_rows': 0, 'num_cols': 0}
                        evidence['elements'].append(table)
                    elif role == 'TR' and table is not None:
                        row = table['num_rows']
                        table['num_rows'] += 1
                    if role in {'TH', 'TD', 'Figure', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6'}:
                        reader.last_read = None
                        text, boxes = content(node, page)
                        by_page = defaultdict(list)
                        for pg, box in boxes:
                            by_page[pg].append(box)
                        # Keep per-page parts for readability, but do not transfer a
                        # multi-page element to a single Docling cell or figure.
                        parts = [{'page': pg, 'bbox': frames[pg].to_docling(union_bbox(drawn))}
                                 for pg, drawn in by_page.items()]
                        item = {'page': parts[0]['page'] if len(parts) == 1 else None,
                                'bbox': parts[0]['bbox'] if len(parts) == 1 else None,
                                'page_parts': parts, 'text': text}
                        a = attrs(node)
                        if role in {'TH', 'TD'} and table is not None:
                            col = sum(c['col_span'] for c in table['cells'] if c['row'] == row)
                            item.update(row=row, col=col, row_span=int(a.get('/RowSpan') or 1),
                                        col_span=int(a.get('/ColSpan') or 1),
                                        is_header=role == 'TH' or a.get('/Scope') is not None,
                                        author_scope=str(a.get('/Scope') or '').lstrip('/'))
                            table['cells'].append(item)
                            table['num_cols'] = max(table['num_cols'], col + item['col_span'])
                        else:
                            item['type'] = 'figure' if role == 'Figure' else 'heading'
                            if role == 'Figure':
                                item['author_alt'] = str(node.get('/Alt') or '')
                                item['author_actual_text'] = str(node.get('/ActualText') or '')
                            else:
                                item['level'] = int(role[1:])
                            evidence['elements'].append(item)
                    walk(node.get('/K'), page, table, row, active)
            walk(root.get('/K'))
        for table in evidence['elements']:
            if table['type'] == 'table' and table['cells']:
                table['page'] = table['cells'][0]['page']
                table['bbox'] = union_bbox(c['bbox'] for c in table['cells'] if c['bbox'])
        return evidence


def _fingerprint(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(text)).casefold() if not c.isspace() and c != '\u00ad')


def _matches(author, target, *, figure=False):
    if author.get('page') != target.get('page'):
        return False
    a, b = author.get('bbox'), target.get('bbox')
    if not a or not b:
        return False
    area_a = max(1, (a['r'] - a['l']) * (a['t'] - a['b']))
    area_b = max(1, (b['r'] - b['l']) * (b['t'] - b['b']))
    intersection = max(0, min(a['r'], b['r']) - max(a['l'], b['l'])) * max(0, min(a['t'], b['t']) - max(a['b'], b['b']))
    if figure:
        return intersection / area_a >= 0.9 and intersection / area_b >= 0.9
    if intersection / min(area_a, area_b) < 0.85:
        return False
    # Full cell text, not a substring: merged or split cells cannot donate headers.
    text = _fingerprint(author.get('text', ''))
    return bool(text) and text == _fingerprint(target.get('text', ''))


def apply_author_structure(structure: dict, evidence: dict) -> dict:
    """Return a copy with one-to-one matches only, keeping an auditable ledger."""
    result = copy.deepcopy(structure)
    audit = result.setdefault('author_structure_audit', [])
    for key in ('language', 'title'):
        if not result.get(key) and evidence.get(key):
            result[key] = evidence[key]
            audit.append({'kind': key, 'value': evidence[key]})
    authors = evidence.get('elements', [])
    targets = []
    for element in result.get('elements', []):
        if element.get('type') == 'table':
            for cell in element.get('cells', []):
                targets.append((cell, {**cell, 'page': element.get('page')}, 'cell'))
        elif element.get('type') in {'figure', 'heading'}:
            targets.append((element, element, element['type']))
    sources = [c for a in authors if a['type'] == 'table' for c in a['cells']]
    sources += [a for a in authors if a['type'] in {'figure', 'heading'}]
    pairs = []
    for i, (_, target, kind) in enumerate(targets):
        for j, author in enumerate(sources):
            if author.get('type', 'cell') == kind and _matches(author, target, figure=kind == 'figure'):
                pairs.append((i, j))
    target_counts, author_counts = Counter(i for i, _ in pairs), Counter(j for _, j in pairs)
    for i, j in pairs:
        if target_counts[i] != 1 or author_counts[j] != 1:
            continue
        target, _, kind = targets[i]
        author = sources[j]
        carried = None
        if kind == 'cell' and author['is_header']:
            target.update(is_header=True, author_scope=author['author_scope'], author_header=True)
            target['row_header'] = author['author_scope'] == 'Row'
            target['column_header'] = author['author_scope'] == 'Column'
            carried = 'TH'
        elif kind == 'figure':
            for field in ('author_alt', 'author_actual_text'):
                if not is_placeholder_alt_text(author[field]):
                    target[field] = author[field]
                    audit.append({'kind': field, 'page': target['page'],
                                  'figure_index': target.get('figure_index'), 'value': author[field]})
        elif kind == 'heading' and not target.get('level'):
            target['level'] = author['level']
            target['author_heading_level'] = author['level']
            carried = 'heading_level'
        if carried:
            audit.append({'kind': carried, 'page': target.get('page', targets[i][1]['page']),
                          'row': target.get('row'), 'col': target.get('col'), 'text': target.get('text'), 'bbox': target.get('bbox')})
    result["author_structure_audit"] = [json.loads(item) for item in dict.fromkeys(json.dumps(e, sort_keys=True) for e in audit)]
    return result
