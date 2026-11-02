"""Recorded figure reply shapes and honest recovery of undescribed figures."""

import pytest
from PIL import Image

from app.pipeline import alt_text
from app.pipeline.orchestrator import _alt_text_entry_state, _apply_figure_reclassification
from app.pipeline.structure import FigureInfo
from app.services import intelligence_figures, intelligence_semantics
from app.services.figure_decisions import normalize_figure_decision
from app.services.semantic_units import SemanticUnit


@pytest.mark.parametrize("field", ["action", "decision", "suggested_action"])
@pytest.mark.parametrize("action", ["set_alt_text", "mark_decorative", "reclassify_region"])
def test_observed_action_fields(field, action):
    result = normalize_figure_decision(
        {field: action, "alt_text": "Three bars rise from left to right."}
    )
    assert result["suggested_action"] == action
    assert result["alt_text"] == "Three bars rise from left to right."


@pytest.mark.asyncio
async def test_semantic_figure_parser_honors_recorded_decoration(monkeypatch):
    async def reply(**kwargs):
        return {"unit_id": "figure-6", "unit_type": "figure", "action": "mark_decorative"}

    monkeypatch.setattr(intelligence_semantics, "request_llm_json", reply)
    unit = SemanticUnit(
        unit_id="figure-6", unit_type="figure", page=1, accessibility_goal="Describe"
    )
    result = await intelligence_semantics.adjudicate_semantic_unit(
        job=None, unit=unit, llm_client=None
    )
    assert result.suggested_action == "mark_decorative"


@pytest.mark.asyncio
async def test_single_decision_batch_maps_only_explicit_candidate(monkeypatch, tmp_path):
    figures = []
    for index in range(13):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (8, 8), "white").save(path)
        figures.append(FigureInfo(index=index, path=path, page=0))
    requests = []
    followups = []

    async def batch(**kwargs):
        import json

        payload = next(
            part["text"]
            for part in kwargs["content"]
            if part.get("text", "").startswith("Context JSON:")
        )
        candidates = json.loads(payload.split("\n", 1)[1])["candidates"]
        requests.append([candidate["figure_index"] for candidate in candidates])
        return {
            "figure_index": candidates[0]["figure_index"],
            "suggested_action": "keep_figure_semantics",
            "set_alt_text": "A red circle above a blue square.",
            "confidence": "high",
        }

    async def single(*, figure, **kwargs):
        followups.append(figure.index)
        return {
            "figure_index": figure.index,
            "suggested_action": "set_alt_text",
            "alt_text": "A blue square below a red circle.",
            "confidence": "high",
        }

    monkeypatch.setattr(intelligence_figures, "request_llm_json", batch)
    monkeypatch.setattr(intelligence_figures, "generate_figure_intelligence", single)
    results = await intelligence_figures.generate_figures_intelligence(
        figures=figures, llm_client=None
    )
    assert [result["figure_index"] for result in results] == list(range(13))
    assert requests == [list(range(i, min(i + 4, 13))) for i in range(0, 13, 4)]
    assert followups == [1, 2, 3, 5, 6, 7, 9, 10, 11]
    assert all(result["alt_text"] for result in results)


def test_uncovered_table_keeps_recorded_description():
    result = alt_text.AltTextResult(
        figure_index=0,
        generated_text="Calendar grid with weekday columns and event dates.",
        status="reclassified",
        resolved_kind="table",
    )
    structure = {"elements": [{"type": "figure", "figure_index": 0, "page": 0}]}
    updated, _ = _apply_figure_reclassification(structure, [result])
    assert updated == structure
    assert _alt_text_entry_state(result, caption=None, auto_approve=True) == (
        "approved",
        result.generated_text,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply,expected",
    [
        (
            {
                "suggested_action": "set_alt_text",
                "alt_text": "A calendar of events arranged in seven weekday columns.",
            },
            "approved",
        ),
        ({"suggested_action": "mark_decorative"}, "rejected"),
        ({"suggested_action": "manual_only", "alt_text": ""}, "pending_review"),
        (
            {"suggested_action": "set_alt_text", "alt_text": "[Generation failed: timeout]"},
            "pending_review",
        ),
    ],
)
async def test_targeted_recovery_uses_pixels_or_preserves_hold(
    monkeypatch, tmp_path, reply, expected
):
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)
    figure = FigureInfo(index=0, path=path, page=0)
    result = alt_text.AltTextResult(
        figure_index=0, generated_text="", status="pending_review", resolved_kind="table"
    )
    calls = []

    async def recover(**kwargs):
        calls.append(kwargs)
        return reply

    monkeypatch.setattr(alt_text, "generate_figure_intelligence", recover)
    await alt_text.describe_unresolved_figures([figure], [result], None, structure={})
    assert result.status == expected
    assert "No tagged table or form" in calls[0]["reviewer_feedback"]
    assert result.generated_text or expected == "pending_review"
    if expected == "pending_review":
        assert _alt_text_entry_state(result, caption=None, auto_approve=True) == (
            "pending_review",
            "",
        )


@pytest.mark.asyncio
async def test_recovery_never_hides_referenced_content(monkeypatch, tmp_path):
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)
    figure = FigureInfo(index=0, path=path, caption="Figure 1. Campus map", page=0)
    result = alt_text.AltTextResult(figure_index=0, generated_text="", status="pending_review")

    async def decorative(**kwargs):
        return {"suggested_action": "mark_decorative"}

    monkeypatch.setattr(alt_text, "generate_figure_intelligence", decorative)
    await alt_text.describe_unresolved_figures([figure], [result], None, structure={})
    assert result.status == "pending_review"


@pytest.mark.asyncio
async def test_failed_single_followup_retains_usable_batch_description(monkeypatch, tmp_path):
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)

    async def batch(**kwargs):
        return {
            "decisions": [
                {
                    "figure_index": 0,
                    "suggested_action": "set_alt_text",
                    "alt_text": "A red circle beside a blue square.",
                    "confidence": "low",
                }
            ]
        }

    async def failed(**kwargs):
        return {"suggested_action": "manual_only", "alt_text": "", "reason": "timeout"}

    monkeypatch.setattr(intelligence_figures, "request_llm_json", batch)
    monkeypatch.setattr(intelligence_figures, "generate_figure_intelligence", failed)
    [result] = await intelligence_figures.generate_figures_intelligence(
        figures=[FigureInfo(index=0, path=path, page=0)],
        llm_client=None,
    )
    assert result["alt_text"] == "A red circle beside a blue square."


@pytest.mark.asyncio
@pytest.mark.parametrize("decorative", [False, True])
async def test_recovery_reaches_the_screen_reader(monkeypatch, tmp_path, decorative):
    from app.pipeline.tagger import tag_pdf
    from tests.test_figure_descriptions import FIGURE_STRUCTURE, _figure_alts, _figure_pdf

    pdf = _figure_pdf(tmp_path / "input.pdf")
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)
    figure = FigureInfo(index=0, path=path, page=0)
    result = alt_text.AltTextResult(figure_index=0, generated_text="", status="pending_review")

    async def recover(**kwargs):
        return {
            "suggested_action": "mark_decorative" if decorative else "set_alt_text",
            "alt_text": "A calendar grid with seven weekday columns.",
        }

    monkeypatch.setattr(alt_text, "generate_figure_intelligence", recover)
    await alt_text.describe_unresolved_figures([figure], [result], None, structure=FIGURE_STRUCTURE)
    status, text = _alt_text_entry_state(result, caption=None, auto_approve=True)
    output = tmp_path / "tagged.pdf"
    await tag_pdf(
        input_path=pdf,
        output_path=output,
        structure_json=FIGURE_STRUCTURE,
        alt_texts=[
            {"figure_index": 0, "status": status, "text": text, "decorative": status == "rejected"}
        ],
        original_filename="input.pdf",
    )
    assert _figure_alts(output) == (
        [] if decorative else ["A calendar grid with seven weekday columns."]
    )


@pytest.mark.asyncio
async def test_image_aware_decoration_can_correct_coarse_classifier(monkeypatch, tmp_path):
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "red").save(path)
    figure = FigureInfo(index=0, path=path, page=0, classification="photograph")

    async def decision(**kwargs):
        return [
            {
                "suggested_action": "mark_decorative",
                "reason": "Repeated red silhouette header ornament.",
                "confidence": "high",
            }
        ]

    monkeypatch.setattr(alt_text, "generate_figures_intelligence", decision)
    [result] = await alt_text.generate_alt_text([figure], None, structure={})
    assert result.status == "rejected"
    assert result.generated_text == "decorative"


@pytest.mark.asyncio
async def test_uncovered_reclassification_retains_batch_description(monkeypatch, tmp_path):
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)

    async def batch(**kwargs):
        return {
            "figure_index": 0,
            "suggested_action": "keep_figure",
            "set_alt_text": "Screenshot of search filters with selected subject checkboxes.",
        }

    async def reclassify(**kwargs):
        return {
            "suggested_action": "reclassify_region",
            "resolved_kind": "form_region",
            "alt_text": "",
        }

    monkeypatch.setattr(intelligence_figures, "request_llm_json", batch)
    monkeypatch.setattr(intelligence_figures, "generate_figure_intelligence", reclassify)
    figure = FigureInfo(index=0, path=path, page=0)
    [decision] = await intelligence_figures.generate_figures_intelligence(
        figures=[figure], llm_client=None
    )
    assert decision["description_source"] == "batch"

    async def decisions(**kwargs):
        return [decision]

    monkeypatch.setattr(alt_text, "generate_figures_intelligence", decisions)
    [result] = await alt_text.generate_alt_text([figure], None)
    _apply_figure_reclassification(
        {"elements": [{"type": "figure", "figure_index": 0, "page": 0}]}, [result]
    )
    assert result.status == "approved"
    assert result.generated_text == "Screenshot of search filters with selected subject checkboxes."


@pytest.mark.parametrize("text", [True, 17, {"text": "A chart"}, ["A chart"]])
def test_nonstring_model_payloads_do_not_become_descriptions(text):
    result = normalize_figure_decision({"action": {"set_alt_text": True}, "alt_text": text})
    assert result["alt_text"] == ""
    assert result["suggested_action"] == "manual_only"


@pytest.mark.asyncio
async def test_request_timeout_during_recovery_keeps_release_hold(monkeypatch, tmp_path):
    import httpx

    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)
    figure = FigureInfo(index=0, path=path, page=0)
    result = alt_text.AltTextResult(figure_index=0, generated_text="", status="pending_review")

    async def timeout(**kwargs):
        raise httpx.ReadTimeout("offline timeout")

    monkeypatch.setattr(intelligence_figures, "adjudicate_semantic_unit", timeout)
    await alt_text.describe_unresolved_figures([figure], [result], None, structure={})
    assert _alt_text_entry_state(result, caption=None, auto_approve=True) == ("pending_review", "")


@pytest.mark.parametrize("field", ["set_alt_text", "resolved_alt_text", "resolved_text"])
def test_observed_description_payload_fields(field):
    result = normalize_figure_decision(
        {"action": "set_alt_text", field: "A face with numbered landmarks."}
    )
    assert result["alt_text"] == "A face with numbered landmarks."


@pytest.mark.parametrize(
    "placeholder", ["N/A", "No description available.", "Unable to describe", "[Generation failed]"]
)
def test_boilerplate_descriptions_and_captions_keep_the_hold(placeholder):
    result = alt_text.AltTextResult(figure_index=0, generated_text=placeholder)
    assert _alt_text_entry_state(result, caption=placeholder, auto_approve=True) == (
        "pending_review",
        "",
    )


@pytest.mark.parametrize('kind', ['text_block', 'layout_grid', '', None])
def test_unsupported_reclassification_stays_described_or_pending(kind):
    result = alt_text.AltTextResult(
        figure_index=7, generated_text='', status='reclassified', resolved_kind=kind,
    )
    structure = {'elements': [{'type':'figure','figure_index':7,'page':0}]}
    updated, _ = _apply_figure_reclassification(structure, [result])
    assert updated == structure
    assert result.status == 'pending_review'
    assert result.reviewable


async def test_recovery_fills_missing_live_result_without_reviving_removed_figures(tmp_path, monkeypatch):
    figures = []
    for index in [2,7,11]:
        path = tmp_path / f'{index}.png'
        Image.new('RGB',(20,20),'white').save(path)
        figures.append(FigureInfo(index=index,path=path,page=0))
    replies = []
    async def recover(*, figure, **kwargs):
        replies.append(figure.index)
        return {'suggested_action':'set_alt_text','alt_text':'Toolbar with graph controls.'}
    monkeypatch.setattr(alt_text,'generate_figure_intelligence',recover)
    results = [alt_text.AltTextResult(figure_index=2,generated_text='Campus map.',status='approved')]
    structure = {'elements':[{'type':'figure','figure_index':i,'page':0} for i in [2,11]]}
    await alt_text.describe_unresolved_figures(figures,results,None,structure=structure)
    assert replies == [11]
    assert {result.figure_index:result.status for result in results} == {2:'approved',11:'approved'}


@pytest.mark.parametrize('failed', [False, True])
def test_missing_docling_crop_keeps_its_index_and_pending_candidate(tmp_path, monkeypatch, failed):
    from app.pipeline.structure import ensure_figure_images
    from app.services import pdf_preview
    from tests.test_figure_descriptions import _figure_pdf
    source = _figure_pdf(tmp_path / 'source.pdf')
    calls = []
    def render(*args, **kwargs):
        calls.append(args)
        if failed:
            raise ValueError('unrenderable page')
        return b'crop'
    monkeypatch.setattr(pdf_preview, 'render_bbox_preview_png_bytes', render)
    structure = {'elements':[{'type':'figure','figure_index':7,'page':0,
                             'bbox':{'l':10,'b':20,'r':30,'t':40}}]}
    [figure] = ensure_figure_images(source,structure,[],tmp_path / 'figures')
    assert figure.index == 7
    assert figure.path.exists() is not failed
    assert calls[0][1] == 1


async def test_description_recovery_excludes_reclassification_and_uses_the_crop(tmp_path, monkeypatch):
    path = tmp_path / 'toolbar.png'
    Image.new('RGB',(60,20),'white').save(path)
    requests = []
    async def request(**kwargs):
        requests.append(kwargs)
        return {'suggested_action':'set_alt_text','alt_text':'Graph controls in a toolbar.',
                'confidence':'high','reason':'Visible controls'}
    async def classify(**kwargs):
        pytest.fail('Description recovery must not repeat the semantic classifier')
    monkeypatch.setattr(intelligence_figures,'request_llm_json',request)
    monkeypatch.setattr(intelligence_figures,'adjudicate_semantic_unit',classify)
    result = alt_text.AltTextResult(figure_index=22,generated_text='',status='reclassified',resolved_kind='form_region')
    structure = {'elements':[{'type':'figure','figure_index':22,'page':0}]}
    structure, _ = _apply_figure_reclassification(structure,[result])
    await alt_text.describe_unresolved_figures([FigureInfo(index=22,path=path,page=0)],
                                             [result],None,structure=structure)
    assert result.status == 'approved'
    assert result.generated_text == 'Graph controls in a toolbar.'
    [request] = requests
    assert set(request['response_schema']['properties']['suggested_action']['enum']) == {
        'set_alt_text','mark_decorative','manual_only',
    }
    assert any(part.get('image_url',{}).get('url','').startswith('data:image/') for part in request['content'])


def _checkered_page(path, *, rotate=0):
    """A page drawing an 8x8 checkerboard image at user box (40, 100)-(120, 180)."""
    import pikepdf

    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 300))
    pixels = bytes(255 * ((x + y) % 2) for y in range(8) for x in range(8))
    image = pdf.make_stream(pixels)
    image.update(
        {
            "/Type": pikepdf.Name("/XObject"),
            "/Subtype": pikepdf.Name("/Image"),
            "/Width": 8,
            "/Height": 8,
            "/ColorSpace": pikepdf.Name("/DeviceGray"),
            "/BitsPerComponent": 8,
        }
    )
    page["/Resources"] = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/Im0": image})})
    page["/Contents"] = pdf.make_stream(b"q 80 0 0 80 40 100 cm /Im0 Do Q\n")
    if rotate:
        page["/Rotate"] = rotate
    pdf.save(path)
    return path


def _docling_box(pdf_path, user_box):
    import pypdfium2 as pdfium

    from app.pipeline.page_frame import PageFrame

    document = pdfium.PdfDocument(str(pdf_path))
    try:
        return PageFrame.of(document[0]).to_docling(user_box)
    finally:
        document.close()


@pytest.mark.parametrize("rotate", [0, 90])
def test_docling_image_showing_less_than_the_page_is_replaced(tmp_path, rotate):
    from app.pipeline.structure import ensure_figure_images

    source = _checkered_page(tmp_path / "source.pdf", rotate=rotate)
    bbox = _docling_box(source, {"l": 40, "b": 100, "r": 120, "t": 180})
    path = tmp_path / "figure_0.png"
    Image.new("RGB", (160, 160), (190, 190, 190)).save(path)
    figure = FigureInfo(index=0, path=path, page=0, bbox=bbox)
    structure = {"elements": [{"type": "figure", "figure_index": 0, "page": 0, "bbox": bbox}]}
    [result] = ensure_figure_images(source, structure, [figure], tmp_path / "figures")
    assert result.path == path
    # The figure now shows the page's checkerboard: each cell black or white,
    # and each unlike its neighbour, whichever way the page is turned.
    with Image.open(path) as image:
        cells = image.convert("L").resize((8, 8), Image.Resampling.BOX)
    dark = [[cells.getpixel((x, y)) < 64 for x in range(8)] for y in range(8)]
    assert all(
        cells.getpixel((x, y)) < 64 or cells.getpixel((x, y)) > 191
        for x in range(8)
        for y in range(8)
    )
    assert all(dark[y][x] != dark[y][x + 1] for y in range(8) for x in range(7))
    assert all(dark[y][x] != dark[y + 1][x] for y in range(7) for x in range(8))


def test_docling_image_matching_the_page_is_kept(tmp_path):
    from app.pipeline.structure import ensure_figure_images

    source = _checkered_page(tmp_path / "source.pdf")
    path = tmp_path / "figure_0.png"
    checks = Image.new("L", (8, 8))
    checks.putdata([255 * ((x + y) % 2) for y in range(8) for x in range(8)])
    checks.resize((160, 160), Image.Resampling.NEAREST).save(path)
    before = path.read_bytes()
    bbox = {"l": 40, "b": 100, "r": 120, "t": 180}
    ensure_figure_images(
        source, {"elements": []}, [FigureInfo(0, path, page=0, bbox=bbox)], tmp_path / "figures"
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize("content", ["flat", "unreadable"])
def test_docling_image_kept_when_the_page_region_is_blank_or_unreadable(tmp_path, content):
    from app.pipeline.structure import ensure_figure_images

    source = _checkered_page(tmp_path / "source.pdf")
    path = tmp_path / "figure_0.png"
    if content == "flat":
        Image.new("RGB", (40, 40), (190, 190, 190)).save(path)
        bbox = {"l": 130, "b": 20, "r": 190, "t": 80}  # empty white page area
    else:
        path.write_bytes(b"not an image")
        bbox = {"l": 40, "b": 100, "r": 120, "t": 180}
    before = path.read_bytes()
    ensure_figure_images(
        source, {"elements": []}, [FigureInfo(0, path, page=0, bbox=bbox)], tmp_path / "figures"
    )
    assert path.read_bytes() == before
