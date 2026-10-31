"""Structure, spoken order and ownership regressions for false table grids."""

import copy

import pikepdf
import pytest

from app.pipeline import tagger
from app.pipeline.fidelity import _output_tables_without_headers
from app.pipeline.page_glyphs import PdfGlyphReader
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import rendered
from tests.test_tagger_tables import _fixture, _tables


def _nodes(node):
    if isinstance(node, pikepdf.Array):
        for child in node:
            yield from _nodes(child)
    elif isinstance(node, pikepdf.Dictionary):
        yield node
        yield from _nodes(node.get('/K'))


def _marked(path):
    with PdfGlyphReader(path) as reader:
        return {key: [g.text if not isinstance(g, str) else g for g in glyphs]
                for key, glyphs in reader.page(0).marked.items()}


async def _compare(tmp_path, monkeypatch, rows, **kwargs):
    source = tmp_path / 'input.pdf'
    structure = _fixture(source, rows, **kwargs)
    return await _compare_structure(tmp_path, monkeypatch, source, structure)


async def _compare_structure(tmp_path, monkeypatch, source, structure):
    before, after = tmp_path / 'before.pdf', tmp_path / 'after.pdf'
    original = copy.deepcopy(structure)
    with monkeypatch.context() as patch:
        patch.setattr(tagger, '_layout_table_plan', lambda *args: None)
        await tagger.tag_pdf(source, before, structure)
    result = await tagger.tag_pdf(source, after, structure)
    assert structure == original
    assert _marked(before) == _marked(after), 'all text retains its stream and MCID'
    assert rendered(before) == rendered(after)
    with pikepdf.open(after) as pdf:
        nodes = list(_nodes(pdf.Root.StructTreeRoot))
        assert not list(_tables(pdf.Root.StructTreeRoot))
        assert not any(str(n.get('/S')) in {'/TR', '/TH', '/TD'} for n in nodes)
        assert all(n.get('/A', {}).get('/O') != '/Table' for n in nodes)
        reachable = {n.objgen for n in nodes if n.is_indirect}
        nums = pdf.Root.StructTreeRoot.ParentTree.Nums
        for parents in list(nums)[1::2]:
            for owner in parents:
                assert owner.objgen in reachable
    assert result.tables_tagged == 0
    assert _output_tables_without_headers(after) == []
    return screen_reader_text(after, markdown=True), after


@pytest.mark.asyncio
@pytest.mark.parametrize('form', [False, True])
async def test_contents_leaders_become_list_preserving_mcids(tmp_path, monkeypatch, form):
    rows = [['A', 'Methods ........ 12'], ['B', 'Findings ........ 17'], ['C', 'Appendix ........ 25']]
    heard, after = await _compare(tmp_path, monkeypatch, rows, form=form)
    assert '<table>' not in heard
    assert heard.index('Methods') < heard.index('Findings') < heard.index('Appendix')
    with pikepdf.open(after) as pdf:
        assert sum(n.get('/S') == '/LI' for n in _nodes(pdf.Root.StructTreeRoot)) == 3


@pytest.mark.asyncio
async def test_bullet_label_column_is_list_with_title(tmp_path, monkeypatch):
    rows = [['Deadlines', None], ['*', 'First application is due'], ['*', 'Second application is due'], ['*', 'Third application is due']]
    heard, after = await _compare(tmp_path, monkeypatch, rows, spans=(0,))
    with pikepdf.open(after) as pdf:
        nodes = list(_nodes(pdf.Root.StructTreeRoot))
        assert sum(n.get('/S') == '/Lbl' for n in nodes) == 3
        (list_node,) = [n for n in nodes if n.get('/S') == '/L']
        assert list_node.A.ListNumbering == '/Disc'
    assert heard.index('Deadlines') < heard.index('First') < heard.index('Second') < heard.index('Third')
    assert heard.count('- ') == 3


@pytest.mark.asyncio
async def test_single_column_marked_items_are_list(tmp_path, monkeypatch):
    heard, _ = await _compare(tmp_path, monkeypatch, [['1. Read the draft'], ['2. Revise the draft'], ['3. Submit the draft']])
    assert heard.count('- ') == 3


@pytest.mark.asyncio
async def test_contents_heading_and_destinations_are_required(tmp_path, monkeypatch):
    source = tmp_path / 'input.pdf'
    rows = [['3.1 First chapter', '2'], ['3.2 Second chapter', '3'], ['3.3 Third chapter', '4']]
    structure = _fixture(source, rows)
    structure['elements'].append(dict(type='heading', page=0, text='Inhalt', level=1,
                                      bbox=dict(l=72, b=725, r=110, t=740)))
    heard, _ = await _compare_structure(tmp_path, monkeypatch, source, structure)
    assert heard.index('First') < heard.index('Second') < heard.index('Third')


@pytest.mark.asyncio
async def test_staggered_prose_columns_read_down_then_across(tmp_path, monkeypatch):
    source = tmp_path / 'input.pdf'
    prose = ' '.join(['word'] * 20)
    rows = [[f'A {prose}', f'C {prose}'], [f'B {prose}', f'D {prose}']]
    structure = _fixture(source, rows)
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        pdf.pages[0].MediaBox = [0, 0, 1200, 792]
        ops = []
        for cell in structure['elements'][0]['cells']:
            row, col = cell['row'], cell['col']
            x, y = 72 + col * 500, 700 - row * 120 - col * 50
            cell['bbox'] = dict(l=x-1, b=y-95, r=x+450, t=y+11)
            ops.append(f"BT /F1 10 Tf {x} {y} Td ({cell['text']}) Tj ET")
        pdf.pages[0].Contents = pdf.make_stream(' '.join(ops).encode())
        pdf.save(source)
    structure['elements'][0]['bbox'] = dict(l=60, b=430, r=1100, t=720)
    heard, _ = await _compare_structure(tmp_path, monkeypatch, source, structure)
    assert heard.index('A word') < heard.index('B word') < heard.index('C word') < heard.index('D word')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['numeric', 'definition', 'sparse', 'headers', 'aligned_prose'])
async def test_ambiguous_and_author_header_grids_remain_tables(tmp_path, kind):
    kwargs = {}
    rows = [['North', '15'], ['South', '20'], ['West', '25']]
    if kind == 'definition':
        rows = [[term, 'A detailed definition with many words explaining what this field means.'] for term in ['Index', 'Name', 'Value']]
    elif kind == 'sparse':
        rows = [['First', 'Value'], ['Second', None], ['Third', 'Other']]
    elif kind == 'headers':
        rows = [['A', 'Methods ........ 12'], ['B', 'Findings ........ 17'], ['C', 'Appendix ........ 25']]
        kwargs['headers'] = {(0, 0): 'Column'}
    elif kind == 'aligned_prose':
        rows = [[' '.join(['word'] * 20)] * 2 for _ in range(3)]
    source, output = tmp_path / 'input.pdf', tmp_path / 'output.pdf'
    structure = _fixture(source, rows, **kwargs)
    await tagger.tag_pdf(source, output, structure)
    with pikepdf.open(output) as pdf:
        assert len(list(_tables(pdf.Root.StructTreeRoot))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['prompts', 'process'])
async def test_author_prompt_and_process_grids_remain_tables(tmp_path, kind):
    # Both shapes occur as author Tables in qb_oers_222, despite the held
    # analysis calling them layouts. Author ground truth takes precedence.
    rows = [[f'{i}. Prompt - explain your reason',
             f'Reply {i} is a full sentence offering a useful example response']
            for i in range(1, 4)]
    if kind == 'process':
        rows = [['- Read', '- Draft', None], ['- Discuss', '- Revise', None],
                ['- Check', '- Submit', 'Goal: Finish'], ['- File', '- Share', None]]
    source, output = tmp_path / 'input.pdf', tmp_path / 'output.pdf'
    structure = _fixture(source, rows)
    await tagger.tag_pdf(source, output, structure)
    with pikepdf.open(output) as pdf:
        assert len(list(_tables(pdf.Root.StructTreeRoot))) == 1
