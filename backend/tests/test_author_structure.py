"""Author semantics survive re-tagging and passing source bytes bypass it."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Job, JobStep
from app.pipeline import alt_text, orchestrator, tagger
from app.pipeline.author_structure import apply_author_structure, read_author_structure
from app.pipeline.structure import FigureInfo
from app.pipeline.validator import ValidationResult
from app.services.structure_text import screen_reader_text
from tests.test_orchestrator_release_decision import _RecordingJobManager
from tests.test_tagger_markinfo import _build_source_alt_figure_pdf, _first_struct_elem
from tests.test_tagger_tables import _fixture, _tables


@pytest.mark.parametrize('form', [False, True])
async def test_author_headers_survive_stripping_and_override_inferred_scope(tmp_path, form):
    source, author, stripped, output = [tmp_path / f'{n}.pdf' for n in ('source', 'author', 'stripped', 'output')]
    structure = _fixture(source, [['Code', 'Amount'], ['A17', '42'], ['B92', '67']],
                         headers={(0, 0): 'Column', (0, 1): 'Column'}, form=form)
    await tagger.tag_pdf(source, author, structure)
    evidence = read_author_structure(author)
    with pikepdf.open(author) as pdf:
        del pdf.Root['/StructTreeRoot']
        pdf.save(stripped)
    docling = copy.deepcopy(structure)
    for cell in docling['elements'][0]['cells']:
        cell.update(is_header=False, column_header=False, row_header=True)
    # Wrong inferred row scope must give way to a matching author Column.
    enriched = apply_author_structure(docling, evidence)
    for cell in enriched['elements'][0]['cells'][2:]:
        cell['row_header'] = False
    assert sum(e['kind'] == 'TH' for e in enriched['author_structure_audit']) == 2
    await tagger.tag_pdf(stripped, output, enriched)
    with pikepdf.open(output) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert all(c.S == '/TH' and c.A.Scope == '/Column' for c in table.K[0].K)
        assert all(c.S == '/TD' for row in list(table.K)[1:] for c in row.K)
    assert screen_reader_text(output) == screen_reader_text(author)
    # A merged/partial cell and ambiguous duplicate must not inherit a TH.
    partial = copy.deepcopy(docling)
    partial['elements'][0]['cells'][0]['text'] = 'Code extra words'
    partial['elements'][0]['cells'].append(copy.deepcopy(partial['elements'][0]['cells'][1]))
    for c in partial['elements'][0]['cells']:
        c.update(is_header=False, row_header=False)
    assert not any(e['kind'] == 'TH' for e in apply_author_structure(partial, evidence)['author_structure_audit'])


def figure_author(path, *, alt='The author describes a gray square.', actual=''):
    _build_source_alt_figure_pdf(path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        node = _first_struct_elem(pdf.Root.StructTreeRoot, 'Figure')
        node.Alt = pikepdf.String(alt)
        if actual:
            node.ActualText = pikepdf.String(actual)
        pdf.save(path)


@pytest.mark.parametrize('kind', ['description', 'placeholder', 'file_name'])
async def test_author_figure_geometry_description_and_placeholder(tmp_path, monkeypatch, kind):
    source, output = tmp_path / 'source.pdf', tmp_path / 'output.pdf'
    placeholder = kind != 'description'
    description = {
        'description': 'The author describes a gray square.',
        'placeholder': '[Could not determine figure semantics]',
        'file_name': 'C:\\Users\\staff\\Pictures\\IMG_1234.jpg',
    }[kind]
    figure_author(source, alt=description, actual='' if placeholder else 'Author replacement reading.')
    structure = {'elements': [dict(type='figure', figure_index=0, page=0,
                                  bbox=dict(l=20, b=20, r=70, t=70))]}
    evidence = read_author_structure(source)
    enriched = apply_author_structure(structure, evidence)
    assert bool(enriched['elements'][0].get('author_alt')) is not placeholder
    # Wrong geometry on the same page must never steal the author's words.
    other = copy.deepcopy(structure)
    other['elements'][0]['bbox'] = dict(l=100, b=100, r=150, t=150)
    assert not apply_author_structure(other, evidence)['elements'][0].get('author_alt')
    if not placeholder:
        async def no_model(**kwargs):
            pytest.fail('Author description should avoid the model')
        monkeypatch.setattr(alt_text, 'generate_figures_intelligence', no_model)
        results = await alt_text.generate_alt_text(
            [FigureInfo(0, tmp_path / 'missing.png', page=0)], SimpleNamespace(model=None),
            structure=enriched,
        )
        assert results[0].generated_text == description
        assert results[0].status == 'approved'
    if kind == 'file_name':
        # A file name is no description: the figure is described as usual.
        image = tmp_path / 'figure.png'
        image.write_bytes(b'png')
        asked = []
        async def model(**kwargs):
            asked.extend(figure.index for figure in kwargs['figures'])
            return [{'suggested_action': 'set_alt_text', 'confidence': 'high',
                     'alt_text': 'A gray square on a white page.'}]
        monkeypatch.setattr(alt_text, 'generate_figures_intelligence', model)
        results = await alt_text.generate_alt_text(
            [FigureInfo(0, image, page=0)], SimpleNamespace(model=None), structure=enriched,
        )
        assert asked == [0]
        assert results[0].generated_text == 'A gray square on a white page.'
    await tagger.tag_pdf(source, output, enriched)
    with pikepdf.open(output) as pdf:
        figure = _first_struct_elem(pdf.Root.StructTreeRoot, 'Figure')
        if placeholder:
            assert not figure.get('/Alt')
        else:
            assert str(figure.Alt) == description
            assert str(figure.ActualText) == 'Author replacement reading.'
    if not placeholder:
        assert 'Author replacement reading.' in screen_reader_text(output)


@pytest.mark.parametrize('failure', [None, 'compliance', 'tagged_text_coverage', 'figure_descriptions',
                                    'table_headers', 'table_readability', 'check_error'])
async def test_original_candidate_release_checks(tmp_path, monkeypatch, failure):
    source = tmp_path / 'author.pdf'
    figure_author(source)
    job = Job(id='job', input_path=str(source), original_filename='author.pdf', classification='digital')
    settings = SimpleNamespace(verapdf_path='verapdf', verapdf_flavour='ua1', subprocess_timeout_validation=30)
    async def validate(**kwargs):
        return ValidationResult(compliant=failure != 'compliance', raw_report={})
    def fidelity(**kwargs):
        checks = [] if failure in {None, 'compliance'} else [{
            'check': 'reading_order' if failure == 'check_error' else failure,
            'status': 'error' if failure == 'check_error' else 'fail',
        }]
        return {'checks': checks}, []
    async def no_tag(**kwargs):
        pytest.fail('Original candidates must never rebuild tags')
    monkeypatch.setattr(orchestrator, 'validate_pdf', validate)
    monkeypatch.setattr(orchestrator, 'assess_fidelity', fidelity)
    monkeypatch.setattr(orchestrator, 'tag_pdf', no_tag)
    original = await orchestrator._evaluate_author_candidate(
        job=job, settings=settings, path=source, evidence=read_author_structure(source),
    )
    retagged = copy.copy(original)
    retagged.produced_by = 'initial'
    selected = orchestrator._prefer_author_candidate(original, retagged)
    assert (selected is original) is (failure is None)
    if failure is not None:
        evidence = read_author_structure(source)
        assert await orchestrator._preserve_author_candidate(
            job=job, settings=settings, evidence=evidence, output_path=tmp_path / 'repaired.pdf',
        ) is None
        assert evidence['author_candidate_audit']['original_passed'] is False
        assert not (tmp_path / 'repaired.pdf').exists()



@pytest.mark.parametrize('repair_passes,change_reading', [(False, False), (True, False), (True, True)])
async def test_passing_original_bypasses_extraction_and_falls_back_if_repairs_fail(tmp_path, monkeypatch, repair_passes, change_reading):
    source = tmp_path / 'author.pdf'
    figure_author(source)
    original_bytes = source.read_bytes()
    async def classify(path):
        return SimpleNamespace(type='digital', total_pages=1, detected_language='en', confidence=1,
                               pages_with_text=0, image_heavy_pages=0, total_image_pixels=1, ocr_scan_like=False)
    async def validate(*, pdf_path, **kwargs):
        return ValidationResult(compliant=Path(pdf_path) == source or repair_passes, raw_report={})
    def fidelity(**kwargs):
        return {'checks': []}, []
    async def no_extract(*args, **kwargs):
        pytest.fail('Passing original must skip Docling')
    monkeypatch.setattr(orchestrator, 'classify_pdf', classify)
    monkeypatch.setattr(orchestrator, 'validate_pdf', validate)
    monkeypatch.setattr(orchestrator, 'assess_fidelity', fidelity)
    monkeypatch.setattr(orchestrator, 'extract_structure', no_extract)
    monkeypatch.setattr(orchestrator, 'create_job_dir', lambda _: tmp_path)
    monkeypatch.setattr(orchestrator, 'get_output_path', lambda _, name: tmp_path / name)
    def repair(input_path, output_path, evidence):
        output_path.write_bytes(input_path.read_bytes())
        if change_reading:
            with pikepdf.open(output_path, allow_overwriting_input=True) as pdf:
                node = _first_struct_elem(pdf.Root.StructTreeRoot, 'Figure')
                node.ActualText = pikepdf.String('Changed reading despite passing thresholds.')
                pdf.save(output_path)
        return [{'repair': 'test', 'applied': False}]
    monkeypatch.setattr(orchestrator, 'repair_author_pdf', repair)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        job = Job(id='job', input_path=str(source), filename='author.pdf', original_filename='author.pdf',
                  owner_session_hash='owner', status='queued')
        db.add(job)
        for step in ('classify', 'ocr', 'structure', 'alt_text', 'tagging', 'validation', 'fidelity'):
            db.add(JobStep(job_id='job', step_name=step))
        await db.commit()
    settings = SimpleNamespace(ocr_language='eng', verapdf_path='verapdf', verapdf_flavour='ua1', subprocess_timeout_validation=30)
    await orchestrator._run_pipeline('job', maker, settings, _RecordingJobManager())
    async with maker() as db:
        job = await db.get(Job, 'job')
        assert job.status == 'complete', job.error
        audit = json.loads(job.validation_json)['candidate_selection']
        assert audit['chosen'] == ('author_repaired' if repair_passes and not change_reading else 'author_original')
        assert Path(job.output_path).read_bytes() == original_bytes
    await engine.dispose()


async def test_author_metadata_and_missing_heading_level_reach_output(tmp_path):
    source, author, output = [tmp_path / f'{name}.pdf' for name in ('source', 'author', 'output')]
    table = _fixture(source, [['Chapter heading']])
    box = table['elements'][0]['cells'][0]['bbox']
    structure = {'title': 'Author title', 'language': 'fr-CA', 'elements': [
        dict(type='paragraph', page=0, text='Chapter heading', bbox=box),
    ]}
    await tagger.tag_pdf(source, author, structure)
    with pikepdf.open(author, allow_overwriting_input=True) as pdf:
        node = _first_struct_elem(pdf.Root.StructTreeRoot, 'P')
        node.S = pikepdf.Name.H3
        pdf.save(author)
    target = {'elements': [dict(type='heading', page=0, text='Chapter heading', bbox=box)]}
    enriched = apply_author_structure(target, read_author_structure(author))
    assert enriched['title'] == 'Author title'
    assert enriched['language'] == 'fr-CA'
    result = await tagger.tag_pdf(source, output, enriched)
    with pikepdf.open(output) as pdf:
        assert _first_struct_elem(pdf.Root.StructTreeRoot, 'H3') is not None
        assert str(pdf.Root.Lang) == 'fr-CA'
        assert str(pdf.docinfo.Title) == 'Author title'
    assert {entry['kind'] for entry in result.author_structure_audit} == {'language', 'title', 'heading_level'}
    # Existing extractor metadata and levels remain authoritative.
    target.update(title='Extractor title', language='de')
    target['elements'][0]['level'] = 2
    enriched = apply_author_structure(target, read_author_structure(author))
    assert enriched['title'] == 'Extractor title'
    assert enriched['language'] == 'de'
    assert enriched['elements'][0]['level'] == 2


async def test_cells_outside_a_table_leave_the_rest_of_the_evidence(tmp_path):
    source, author = tmp_path / 'source.pdf', tmp_path / 'author.pdf'
    table = _fixture(source, [['Chapter heading']])
    box = table['elements'][0]['cells'][0]['bbox']
    await tagger.tag_pdf(source, author, {'elements': [
        dict(type='paragraph', page=0, text='Chapter heading', bbox=box),
    ]})
    with pikepdf.open(author, allow_overwriting_input=True) as pdf:
        heading = _first_struct_elem(pdf.Root.StructTreeRoot, 'P')
        heading.S = pikepdf.Name.H3
        # A row inside a paragraph, as one archive PDF has: its cells have no grid.
        cells = [pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.StructElem, S=pikepdf.Name(role), K=pikepdf.Array()))
            for role in ('/TH', '/TD')]
        row = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.StructElem, S=pikepdf.Name.TR, K=pikepdf.Array(cells)))
        paragraph = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.StructElem, S=pikepdf.Name.P, K=pikepdf.Array([row]), P=heading.P))
        for cell in cells:
            cell.P = row
        row.P = paragraph
        parent = heading.P
        parent.K = pikepdf.Array([*parent.K, paragraph]) if isinstance(parent.K, pikepdf.Array) else pikepdf.Array([parent.K, paragraph])
        pdf.save(author)
    evidence = read_author_structure(author)
    assert [(e['type'], e.get('level'), e['text']) for e in evidence['elements']] == [('heading', 3, 'Chapter heading')]


async def test_scope_on_author_td_is_header_evidence(tmp_path):
    source, author, output = [tmp_path / f'{name}.pdf' for name in ('source', 'author', 'output')]
    structure = _fixture(source, [['Code', 'Amount'], ['A17', '42'], ['B92', '67']],
                         headers={(0, 0): 'Column', (0, 1): 'Column'})
    await tagger.tag_pdf(source, author, structure)
    with pikepdf.open(author, allow_overwriting_input=True) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        for cell in table.K[0].K:
            cell.S = pikepdf.Name.TD
            cell.A.Scope = pikepdf.Name.Both
        pdf.save(author)
    for cell in structure['elements'][0]['cells']:
        cell.update(is_header=False, column_header=False, row_header=False)
    enriched = apply_author_structure(structure, read_author_structure(author))
    result = await tagger.tag_pdf(source, output, enriched)
    with pikepdf.open(output) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert all(c.S == '/TH' and c.A.Scope == '/Both' for c in table.K[0].K)
    assert sum(e['kind'] == 'TH' for e in result.author_structure_audit) == 2


def test_author_repairs_keep_structure_and_rendering(tmp_path):
    from app.pipeline.author_repairs import repair_author_pdf
    from tests.pdf_fixtures import rendered

    source, output = tmp_path / 'author.pdf', tmp_path / 'repaired.pdf'
    figure_author(source, actual='The author replacement.')
    before = read_author_structure(source)
    repair_author_pdf(source, output, before)
    assert read_author_structure(output) == before
    assert screen_reader_text(output) == screen_reader_text(source)
    assert rendered(output) == rendered(source)


@pytest.mark.parametrize(
    'raw,clean',
    [
        ('gcc logo\x00', 'gcc logo'),
        ('A blue text on a white background\n\nAI-generated content may be incorrect.',
         'A blue text on a white background AI-generated content may be incorrect.'),
        ('Know where to get help\x0b      ', 'Know where to get help'),
        ('Robustness and resilience of complex networks\r', 'Robustness and resilience of complex networks'),
        ('CUNY\tAcademic\x07 Works\x1f', 'CUNY Academic Works'),
        # Only control characters go: other marks and letters stay.
        ('Café — naïve ✓ x​y', 'Café — naïve ✓ x​y'),
    ],
)
def test_author_text_drops_control_characters(raw, clean):
    from app.services.pdf_objects import author_text

    assert author_text(pikepdf.String(raw)) == clean


def _author_text_everywhere(path):
    """A tagged figure, a link, a note, a form field and bookmarks whose
    author text carries the control characters Word leaves behind."""
    figure_author(path, alt='gcc logo\x00', actual='Author replacement\nreading.\x00')
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        link = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link,
                                  Rect=[100, 100, 150, 120], Contents=pikepdf.String('CUNY\nAcademic Works'),
                                  A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String('https://x.org')))
        note = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Text,
                                  Rect=[160, 160, 180, 180], Contents=pikepdf.String('Check\x00'),
                                  T=pikepdf.String('J. Author\x00'))
        field = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, FT=pikepdf.Name.Tx,
                                   Rect=[20, 150, 90, 170], T=pikepdf.String('name\x00'),
                                   TU=pikepdf.String('Your name\x00'))
        # A radio group is a field of its own, named apart from its widgets.
        group = pdf.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Btn, Ff=49152, T=pikepdf.String('choice'),
                                                     TU=pikepdf.String('Pick one\x00')))
        option = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget,
                                                      Rect=[20, 120, 30, 130], Parent=group))
        group.Kids = pikepdf.Array([option])
        annots = [pdf.make_indirect(a) for a in (link, note, field)]
        page.Annots = pikepdf.Array([*annots, option])
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([annots[2], group]))
        outlines = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Outlines))
        first = pdf.make_indirect(pikepdf.Dictionary(Title=pikepdf.String('Introduction\r'), Parent=outlines,
                                                     Dest=pikepdf.Array([page.obj, pikepdf.Name.Fit])))
        second = pdf.make_indirect(pikepdf.Dictionary(Title=pikepdf.String('Get help\x0b   '), Parent=outlines,
                                                      Prev=first, Dest=pikepdf.Array([page.obj, pikepdf.Name.Fit])))
        first.Next = second
        outlines.First, outlines.Last, outlines.Count = first, second, 2
        pdf.Root.Outlines = outlines
        pdf.docinfo['/Title'] = pikepdf.String('Annual report\x00')
        pdf.save(path)


async def test_author_text_reaches_the_output_without_control_characters(tmp_path):
    source, output = tmp_path / 'source.pdf', tmp_path / 'output.pdf'
    _author_text_everywhere(source)
    structure = {'elements': [dict(type='figure', figure_index=0, page=0,
                                  bbox=dict(l=20, b=20, r=70, t=70))]}
    enriched = apply_author_structure(structure, read_author_structure(source))
    assert enriched['elements'][0]['author_alt'] == 'gcc logo'
    assert enriched['title'] == 'Annual report'
    await tagger.tag_pdf(source, output, enriched)
    with pikepdf.open(output) as pdf:
        figure = _first_struct_elem(pdf.Root.StructTreeRoot, 'Figure')
        assert str(figure.Alt) == 'gcc logo'
        assert str(figure.ActualText) == 'Author replacement reading.'
        link, note, field, option = (a for a in pdf.pages[0].Annots)
        assert str(option.Parent.TU) == 'Pick one'
        assert str(link.Contents) == 'CUNY Academic Works'
        assert (str(note.Contents), str(note.T)) == ('Check', 'J. Author')
        assert str(field.TU) == 'Your name'
        # A field's partial name identifies it to scripts and submissions.
        assert str(field.T) == 'name\x00'
        titles = []
        item = pdf.Root.Outlines.get('/First')
        while item is not None:
            titles.append(str(item.Title))
            item = item.get('/Next')
        assert titles[:2] == ['Introduction', 'Get help']
        assert str(pdf.docinfo['/Title']) == 'Annual report'


async def test_stored_description_reaches_alt_without_control_characters(tmp_path):
    from tests.test_figure_descriptions import FIGURE_STRUCTURE, _figure_alts, _figure_pdf

    output = tmp_path / 'tagged.pdf'
    await tagger.tag_pdf(
        input_path=_figure_pdf(tmp_path / 'in.pdf'), output_path=output,
        structure_json=FIGURE_STRUCTURE,
        alt_texts=[{'figure_index': 0, 'text': 'A gray square\x00\n', 'status': 'approved'}],
    )
    assert _figure_alts(output) == ['A gray square']


def test_a_kid_widget_named_from_its_field_name_has_no_control_characters():
    from app.services.form_fields import name_widget_field

    with pikepdf.new() as pdf:
        field = pdf.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Btn, T=pikepdf.String('Agree\x00')))
        widget = pdf.make_indirect(pikepdf.Dictionary(Subtype=pikepdf.Name.Widget, Parent=field))
        field.Kids = pikepdf.Array([widget])
        name_widget_field(widget)
        assert str(field.TU) == 'Agree'
        assert str(field.T) == 'Agree\x00'


@pytest.mark.parametrize('where', ['info', 'xmp'])
def test_document_title_from_author_metadata_has_no_control_characters(tmp_path, where):
    from app.pipeline.document_title import select_document_title

    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        if where == 'info':
            pdf.docinfo['/Title'] = pikepdf.String('Annual report\x00')
        else:
            with pdf.open_metadata() as metadata:
                metadata['dc:title'] = 'Annual report\x0b'
        decision = select_document_title(None, {'elements': []}, 'scan.pdf', source_pdf=pdf)
    assert decision['title'] == 'Annual report'


async def test_a_stored_structure_keeps_no_control_characters_in_author_text(tmp_path):
    source, output = tmp_path / 'source.pdf', tmp_path / 'output.pdf'
    figure_author(source, alt='')
    # A structure saved before author text was cleaned when read.
    stored = {'elements': [dict(type='figure', figure_index=0, page=0, bbox=dict(l=20, b=20, r=70, t=70),
                                author_alt='Graduation cap\x00', author_actual_text='Cap\x00')]}
    await tagger.tag_pdf(source, output, stored)
    with pikepdf.open(output) as pdf:
        figure = _first_struct_elem(pdf.Root.StructTreeRoot, 'Figure')
        assert (str(figure.Alt), str(figure.ActualText)) == ('Graduation cap', 'Cap')
