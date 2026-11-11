"""Local figure decisions and persistent crop descriptions avoid model work."""

import base64
import hashlib
import json
import sqlite3
import tarfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pikepdf
import pytest
from PIL import Image

from app.config import Settings
from app.pipeline import alt_text
from app.pipeline.structure import FigureInfo, _save_docling_figures
from app.runner.state import pack_state, remove_job_files
from app.services.alt_text_cache import MAX_AGE_NS, AltTextCache, request_input_hash
from app.services.intelligence_figures import figure_description_guidance

DESCRIPTION = "Blue square with a white center."


class FakeLlmClient:
    model = "fake-vision"

    def __init__(self, *, fail=False, text=DESCRIPTION):
        self.calls = []
        self.fail = fail
        self.text = text

    async def prepare_request(self, messages, **kwargs):
        from app.config import DEFAULT_LLM_MAX_REQUEST_BYTES
        from app.services.vision_requests import bound_vision_request

        return bound_vision_request({"model": self.model, "messages": messages, **kwargs},
                                    DEFAULT_LLM_MAX_REQUEST_BYTES)

    async def chat_completion(self, messages, **kwargs):
        self.calls.append(messages)
        if self.fail:
            raise httpx.ConnectError("offline test failure")
        context = next(
            json.loads(part["text"].split("Context JSON:\n", 1)[1])
            for part in messages[-1]["content"]
            if part.get("text", "").startswith("Context JSON:\n")
        )
        decisions = [
            {"figure_index": candidate["figure_index"], "confidence": "high",
             "summary": "Visible content.", "reason": "Grounded in crop.",
             "suggested_action": "set_alt_text", "alt_text": self.text}
            for candidate in context["candidates"]
        ]
        return {"choices": [{"message": {"content": json.dumps({"decisions": decisions})}}]}


@pytest.fixture(autouse=True)
def isolated_model_capabilities(monkeypatch):
    # Each fake provider starts with its own capability negotiation.
    from app.services import intelligence_llm_utils

    monkeypatch.setattr(intelligence_llm_utils, "FORMAT_CAPABILITIES", intelligence_llm_utils.FormatCapabilityCache())


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None, llm_strict_validation=False,
        upload_dir=tmp_path / "uploads", processing_dir=tmp_path / "processing",
        output_dir=tmp_path / "output",
    )


def figure(tmp_path, *, index=0, page=0, box=None, alpha=255, color="blue", caption=None, kind=None):
    path = tmp_path / f"figure-{index}.png"
    image = Image.new("RGBA", (40, 40), color)
    image.putalpha(alpha)
    image.save(path)
    return FigureInfo(
        index=index, path=path, page=page, caption=caption, classification=kind,
        bbox=box or {"l": 40, "b": 40, "r": 140, "t": 140},
    )


def pdf_pages(path, *, count=1, cover=None, reverse=False, graphics_state=False, pattern=False):
    pdf = pikepdf.new()
    for _ in range(count):
        page = pdf.add_blank_page(page_size=(600, 800))
        image = pdf.make_stream(bytes([90]))
        image["/Type"] = pikepdf.Name("/XObject")
        image["/Subtype"] = pikepdf.Name("/Image")
        image["/Width"] = image["/Height"] = 1
        image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
        image["/BitsPerComponent"] = 8
        page["/Resources"] = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/Im0": image})})
        draw = b"q 100 0 0 100 40 40 cm /Im0 Do Q\n"
        fill = f"{cover} re f\n".encode() if cover else b""
        if pattern:
            fill = b"/Pattern cs /P scn\n" + fill
        if graphics_state:
            page.Resources["/ExtGState"] = pikepdf.Dictionary({"/GS": pikepdf.Dictionary({"/ca": 0.5})})
            fill = b"/GS gs\n" + fill
        page["/Contents"] = pdf.make_stream(fill + draw if reverse else draw + fill)
    pdf.save(path)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["tiny_image", "repeated_image", "invisible_image", "fully_covered"])
async def test_decorative_rule_skips_model_and_records_reason(tmp_path, settings, rule):
    pdf_path = pdf_pages(tmp_path / "input.pdf", count=3, cover="40 40 100 100" if rule == "fully_covered" else None)
    if rule == "tiny_image":
        figures = [figure(tmp_path, box={"l": 40, "b": 40, "r": 51, "t": 51})]
    elif rule == "repeated_image":
        figures = [figure(tmp_path, index=i, page=i, box={"l": 40+i, "b": 20, "r": 100+i, "t": 40}) for i in range(3)]
        # Encoded bytes differ; identical decoded pixels still prove repetition.
        with Image.open(figures[1].path) as image:
            image.save(figures[1].path, compress_level=0)
    else:
        figures = [figure(tmp_path, alpha=0 if rule == "invisible_image" else 255)]
    client = FakeLlmClient()
    results = await alt_text.generate_alt_text(figures, client, settings=settings, pdf_path=pdf_path)
    assert client.calls == []
    assert all(result.status == "rejected" for result in results)
    specs = alt_text.figure_applied_change_specs(figures=figures, alt_texts=results)
    assert all(spec["metadata"]["remediation_intelligence"]["rule"] == rule for spec in specs)
    assert all(spec["metadata"]["reason"].startswith(rule + ":") for spec in specs)
    assert all(spec["reviewable"] for spec in specs)


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["tiny", "repeated", "invisible", "covered"])
@pytest.mark.parametrize("protection", ["caption", "reference", "classified_reference"])
async def test_captioned_and_referenced_figures_are_never_decorative(tmp_path, settings, rule, protection):
    box = {"l": 40, "b": 20, "r": 100, "t": 40} if rule == "repeated" else None
    if rule == "tiny":
        box = {"l": 40, "b": 40, "r": 51, "t": 51}
    figures = [figure(
        tmp_path, index=i, page=i, box=box, alpha=0 if rule == "invisible" else 255,
        caption="Figure 3. Visible detail" if protection == "caption" else None,
        kind="bar_chart" if protection == "classified_reference" else None,
    ) for i in range(3 if rule == "repeated" else 1)]
    structure = {"elements": [{"page": i, "text": "See Figure 3 for details."} for i in range(3)]} if protection != "caption" else {}
    pdf_path = pdf_pages(tmp_path / "input.pdf", count=3, cover="40 40 100 100" if rule == "covered" else None)
    client = FakeLlmClient()
    results = await alt_text.generate_alt_text(figures, client, settings=settings, structure=structure, pdf_path=pdf_path)
    assert client.calls
    assert all(result.status == "approved" for result in results)
    assert all(result.generated_text == DESCRIPTION for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["one_side_large", "threshold", "two_pages", "same_page", "moved", "different_pixels", "body_content", "visible_alpha", "partial_cover", "cover_before", "transparent_cover", "pattern_cover"])
async def test_ambiguous_images_still_request_descriptions(tmp_path, settings, case):
    box = {"l": 40, "b": 20, "r": 100, "t": 40}
    if case == "one_side_large":
        box = {"l": 40, "b": 40, "r": 51, "t": 100}
    elif case == "threshold":
        box = {"l": 40, "b": 40, "r": 52, "t": 52}
    elif case in {"partial_cover", "cover_before", "transparent_cover", "pattern_cover"}:
        box = None
    count = 2 if case == "two_pages" else 3 if case in {"same_page", "moved", "different_pixels", "body_content"} else 1
    if case == "body_content":
        box = {"l": 40, "b": 300, "r": 100, "t": 320}
    figures = [figure(tmp_path, index=i, page=0 if case == "same_page" else i, box=box,
                      alpha=1 if case == "visible_alpha" else 255,
                      color="red" if case == "different_pixels" and i == 2 else "blue") for i in range(count)]
    if case == "moved":
        figures[2].bbox = {"l": 43, "b": 20, "r": 103, "t": 40}
    cover = "40 40 99 100" if case == "partial_cover" else "40 40 100 100" if case in {"cover_before", "transparent_cover", "pattern_cover"} else None
    pdf_path = pdf_pages(tmp_path / "input.pdf", count=3, cover=cover, reverse=case == "cover_before", graphics_state=case == "transparent_cover", pattern=case == "pattern_cover")
    client = FakeLlmClient()
    results = await alt_text.generate_alt_text(figures, client, settings=settings, pdf_path=pdf_path)
    assert client.calls
    assert all(result.status == "approved" for result in results)


@pytest.mark.asyncio
async def test_cache_hit_avoids_model_and_keeps_only_text_across_jobs(tmp_path, settings):
    fig = figure(tmp_path, caption="PRIVATE CAPTION")
    client = FakeLlmClient()
    [first] = await alt_text.generate_alt_text([fig], client, settings=settings, original_filename="private-job.pdf")
    other_job_dir = tmp_path / "other-job"
    other_job_dir.mkdir()
    other_image = other_job_dir / "same-image.png"
    other_image.write_bytes(fig.path.read_bytes())
    [second] = await alt_text.generate_alt_text(
        [replace(fig, path=other_image)], client, settings=settings, original_filename="private-job.pdf",
    )
    assert len(client.calls) == 1
    assert first.generated_text == second.generated_text == DESCRIPTION
    assert second.figure_index == fig.index
    assert second.reviewable
    assert second.remediation_intelligence["cached"] is True
    cache_path = settings.processing_dir.parent / "alt_text_cache.sqlite3"
    with sqlite3.connect(cache_path) as connection:
        [row] = connection.execute("SELECT * FROM descriptions").fetchall()
    assert row[:3] == (hashlib.sha256(fig.path.read_bytes()).hexdigest(), client.model, alt_text.PROMPT_VERSION)
    assert len(row[3]) == 64 and int(row[3], 16) >= 0
    assert row[4] == DESCRIPTION
    assert len(row) == 7
    assert isinstance(row[5], int)
    assert isinstance(row[6], int)
    assert b"PRIVATE CAPTION" not in cache_path.read_bytes()
    assert b"private-job" not in cache_path.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "empty", "bracketed", "manual", "decorative", "reclassified"])
async def test_failures_and_non_descriptions_are_never_cached(tmp_path, settings, monkeypatch, failure):
    fig = figure(tmp_path)
    client = FakeLlmClient(fail=failure == "exception", text="[Generation failed]" if failure == "bracketed" else "")
    if failure in {"empty", "manual", "decorative", "reclassified"}:
        async def fake_intelligence(**kwargs):
            return [{"suggested_action": {"manual": "manual_only", "decorative": "mark_decorative", "reclassified": "reclassify_region"}.get(failure, "set_alt_text"),
                     "alt_text": DESCRIPTION if failure == "manual" else "", "confidence": "high",
                     "resolved_kind": "table" if failure == "reclassified" else None}]
        monkeypatch.setattr(alt_text, "generate_figures_intelligence", fake_intelligence)
    first = await alt_text.generate_alt_text([fig], client, settings=settings)
    calls = len(client.calls)
    second = await alt_text.generate_alt_text([fig], client, settings=settings)
    assert not (second[0].remediation_intelligence or {}).get("cached")
    cache_path = settings.processing_dir.parent / "alt_text_cache.sqlite3"
    if cache_path.exists():
        with sqlite3.connect(cache_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM descriptions").fetchone()[0] == 0
    if failure in {"exception", "bracketed"}:
        assert len(client.calls) > calls
        assert first[0].status == second[0].status == "pending_review"
        assert second[0].generated_text == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["prompt", "model", "bytes"])
async def test_cache_key_change_requests_a_new_description(tmp_path, settings, monkeypatch, changed):
    fig = figure(tmp_path)
    client = FakeLlmClient()
    await alt_text.generate_alt_text([fig], client, settings=settings)
    await alt_text.generate_alt_text([fig], client, settings=settings)
    assert len(client.calls) == 1
    if changed == "prompt":
        monkeypatch.setattr(alt_text, "PROMPT_VERSION", alt_text.PROMPT_VERSION + "-next")
    elif changed == "model":
        client.model = "another-model"
    else:
        Image.new("RGB", (40, 40), "red").save(fig.path)
    [result] = await alt_text.generate_alt_text([fig], client, settings=settings)
    assert len(client.calls) == 2
    assert not (result.remediation_intelligence or {}).get("cached")


def test_cache_evicts_least_recently_used_and_serializes_threads(tmp_path):
    cache = AltTextCache(tmp_path / "cache.sqlite3", max_entries=2)
    cache.put("a", "model", "v1", "input", "First description")
    cache.put("b", "model", "v1", "input", "Second description")
    assert cache.get("a", "model", "v1", "input")
    cache.put("c", "model", "v1", "input", "Third description")
    assert cache.get("b", "model", "v1", "input") is None
    assert cache.get("a", "model", "v1", "input") == "First description"

    def roundtrip(index):
        other = AltTextCache(cache.path, max_entries=100)
        other.put(str(index), "model", "v1", "input", f"Description {index}")
        return other.get(str(index), "model", "v1", "input")

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(roundtrip, range(20))) == [f"Description {i}" for i in range(20)]


@pytest.mark.asyncio
async def test_runner_cleanup_and_archives_exclude_shared_cache(tmp_path, settings):
    client = FakeLlmClient()
    fig = figure(tmp_path)
    await alt_text.generate_alt_text([fig], client, settings=settings)
    job_dir = settings.processing_dir / "job-one"
    job_dir.mkdir(parents=True)
    (job_dir / "state.txt").write_text("job state")
    archive = pack_state(settings, "job-one", tmp_path / "state.tar.gz")
    with tarfile.open(archive) as tar:
        assert all("alt_text_cache" not in name for name in tar.getnames())
    remove_job_files(settings, "job-one")
    [result] = await alt_text.generate_alt_text([fig], client, settings=settings)
    assert result.remediation_intelligence["cached"]
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [("bar_chart", "axes"), ("flow_chart", "relationships"), ("photograph", "subjects"), ("screenshot", "interface"), ("logo", "visible name"), ("equation", "speakable")])
async def test_classification_selects_guidance_in_existing_request(tmp_path, settings, kind, expected):
    client = FakeLlmClient()
    await alt_text.generate_alt_text([figure(tmp_path, kind=kind)], client, settings=settings)
    assert len(client.calls) == 1
    assert expected in figure_description_guidance(kind)
    assert figure_description_guidance(kind) in str(client.calls)


@pytest.mark.parametrize("modern", [True, False])
def test_structure_preserves_picture_classification(tmp_path, modern):
    fig = figure(tmp_path)
    prediction = {"class_name": "bar_chart", "confidence": 0.9}
    picture = {"prov": [{"page_no": 1, "bbox": fig.bbox}],
               "image": {"uri": "data:image/png;base64," + base64.b64encode(fig.path.read_bytes()).decode()}}
    if modern:
        picture["meta"] = {"classification": {"predictions": [prediction]}}
    else:
        picture["annotations"] = [{"kind": "classification", "predicted_classes": [prediction]}]
    [saved] = _save_docling_figures({"pictures": [picture]}, tmp_path / "figures")
    assert saved.classification == "bar_chart"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["mark_decorative", "reclassify_region"])
async def test_protected_figure_cannot_be_hidden_by_model(tmp_path, settings, monkeypatch, action):
    async def hide(**kwargs):
        return [{"suggested_action": action, "resolved_kind": "artifact", "is_decorative": action == "mark_decorative", "confidence": "high"}]
    monkeypatch.setattr(alt_text, "generate_figures_intelligence", hide)
    [result] = await alt_text.generate_alt_text([figure(tmp_path, caption="Figure 3. Results")], FakeLlmClient(), settings=settings)
    assert result.status == "approved"
    assert result.generated_text == "Figure 3. Results"
    assert result.used_caption_fallback


@pytest.mark.asyncio
async def test_missing_tiny_image_still_needs_a_description(tmp_path, settings):
    fig = figure(tmp_path, box={"l": 40, "b": 40, "r": 51, "t": 51})
    fig.path.unlink()
    client = FakeLlmClient()
    [result] = await alt_text.generate_alt_text([fig], client, settings=settings)
    assert result.status == "pending_review"
    assert result.generated_text == ""
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["caption", "classification", "filename", "bbox", "page_context", "page_image", "prompt", "image_detail", "schema"])
async def test_same_image_with_different_model_context_is_a_cache_miss(tmp_path, settings, monkeypatch, changed):
    from app.services import intelligence_figures

    fig = figure(tmp_path, caption="PRIVATE CAPTION", kind="photograph")
    client = FakeLlmClient()
    page_preview = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,original-page"}}]
    if changed == "page_image":
        monkeypatch.setattr(intelligence_figures, "page_preview_parts", lambda *_: page_preview)
    await alt_text.generate_alt_text([fig], client, settings=settings, original_filename="first.pdf")
    # Establish that caching is active, then change exactly one input field.
    await alt_text.generate_alt_text([fig], client, settings=settings, original_filename="first.pdf")
    assert len(client.calls) == 1
    filename = "first.pdf"
    if changed == "caption":
        fig = replace(fig, caption="DIFFERENT PRIVATE CAPTION")
    elif changed == "classification":
        fig = replace(fig, classification="diagram")
    elif changed == "filename":
        filename = "different.pdf"
    elif changed == "bbox":
        fig = replace(fig, bbox={**fig.bbox, "r": 141})
    elif changed == "page_context":
        original_context = intelligence_figures._figure_page_context
        def with_nearby_text(figures):
            context = original_context(figures)
            context[fig.index]["nearby_text"] = "PRIVATE NEARBY TEXT"
            return context
        monkeypatch.setattr(intelligence_figures, "_figure_page_context", with_nearby_text)
    elif changed == "page_image":
        page_preview[0]["image_url"]["url"] = "data:image/jpeg;base64,changed-page"
    elif changed == "prompt":
        monkeypatch.setattr(intelligence_figures, "FIGURE_BATCH_PROMPT", intelligence_figures.FIGURE_BATCH_PROMPT + " Changed instruction.")
    elif changed == "image_detail":
        original_request = intelligence_figures.request_llm_json
        async def changed_detail(**kwargs):
            for part in kwargs["content"]:
                if part.get("type") == "image_url":
                    part["image_url"]["detail"] = "low"
            return await original_request(**kwargs)
        monkeypatch.setattr(intelligence_figures, "request_llm_json", changed_detail)
    else:
        monkeypatch.setattr(intelligence_figures, "FIGURE_BATCH_SCHEMA", {**intelligence_figures.FIGURE_BATCH_SCHEMA, "description": "Changed schema instruction"})
    [result] = await alt_text.generate_alt_text([fig], client, settings=settings, original_filename=filename)
    assert len(client.calls) == 2
    assert not result.remediation_intelligence.get("cached")
    # No context text is written to any row or SQLite page.
    cache_path = settings.processing_dir.parent / "alt_text_cache.sqlite3"
    for private in (b"PRIVATE CAPTION", b"PRIVATE NEARBY TEXT", b"first.pdf", b"different.pdf"):
        assert private not in cache_path.read_bytes()


@pytest.mark.asyncio
async def test_multi_figure_batch_results_are_not_cached(tmp_path, settings):
    figures = [figure(tmp_path, index=0, caption="First caption"), figure(tmp_path, index=1, caption="Other person's context")]
    client = FakeLlmClient()
    await alt_text.generate_alt_text(figures, client, settings=settings)
    results = await alt_text.generate_alt_text(figures, client, settings=settings)
    assert len(client.calls) == 2
    assert all(not result.remediation_intelligence.get("cached") for result in results)
    assert not (settings.processing_dir.parent / "alt_text_cache.sqlite3").exists()
    # A figure from that shared batch must not reuse its context in isolation.
    await alt_text.generate_alt_text([figures[0]], client, settings=settings)
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_cached_singleton_does_not_enter_a_shared_batch(tmp_path, settings):
    first = figure(tmp_path, index=0, caption="First caption")
    second = figure(tmp_path, index=1, caption="Shared batch context")
    client = FakeLlmClient()
    await alt_text.generate_alt_text([first], client, settings=settings)
    results = await alt_text.generate_alt_text([first, second], client, settings=settings)
    assert len(client.calls) == 2
    assert all(not result.remediation_intelligence.get("cached") for result in results)
    [result] = await alt_text.generate_alt_text([first], client, settings=settings)
    assert result.remediation_intelligence["cached"]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_single_figure_followup_results_are_not_cached(tmp_path, settings, monkeypatch):
    from app.services import intelligence_figures

    async def no_batch_decision(**kwargs):
        return {"decisions": []}
    async def successful_followup(**kwargs):
        return {"figure_index": 0, "suggested_action": "set_alt_text", "confidence": "high", "alt_text": DESCRIPTION}
    monkeypatch.setattr(intelligence_figures, "request_llm_json", no_batch_decision)
    monkeypatch.setattr(intelligence_figures, "generate_figure_intelligence", successful_followup)
    [result] = await alt_text.generate_alt_text([figure(tmp_path)], FakeLlmClient(), settings=settings)
    assert result.generated_text == DESCRIPTION
    assert not (settings.processing_dir.parent / "alt_text_cache.sqlite3").exists()


def test_context_free_legacy_cache_rows_are_discarded(tmp_path):
    path = tmp_path / "cache.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE descriptions (image_hash TEXT, model TEXT, prompt_version TEXT, description TEXT, accessed INTEGER)")
        connection.execute("INSERT INTO descriptions VALUES ('image', 'model', 'v1', 'Private legacy description', 1)")
    cache = AltTextCache(path)
    assert cache.get("image", "model", "v1", "new-input-hash") is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM descriptions").fetchone()[0] == 0
        assert "input_hash" in {row[1] for row in connection.execute("PRAGMA table_info(descriptions)")}


def test_request_hash_retains_exact_text_and_hashes_all_inline_images():
    content = [{"type": "text", "text": "PRIVATE CONTEXT"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,pixels", "detail": "high"}}]
    payload = {"messages": [{"role": "user", "content": content}], "temperature": 0}
    before = json.dumps(payload)
    digest = request_input_hash(payload)
    assert len(digest) == 64
    assert json.dumps(payload) == before
    content[0]["text"] += " "
    assert request_input_hash(payload) != digest
    content[0]["text"] = "PRIVATE CONTEXT"
    content[1]["image_url"]["url"] = "data:image/png;base64,other-pixels"
    assert request_input_hash(payload) != digest
    content[1]["image_url"]["url"] = "https://example.invalid/image.png"
    assert request_input_hash(payload) is None


@pytest.mark.asyncio
async def test_repaired_json_responses_are_not_cached(tmp_path, settings):
    class RepairClient(FakeLlmClient):
        async def chat_completion(self, messages, **kwargs):
            if len(self.calls) % 2 == 0:
                self.calls.append(messages)
                return {"choices": [{"message": {"content": "not JSON"}}]}
            return await super().chat_completion(messages, **kwargs)

    client = RepairClient()
    fig = figure(tmp_path)
    for _ in range(2):
        [result] = await alt_text.generate_alt_text([fig], client, settings=settings)
        assert result.generated_text == DESCRIPTION
        assert not result.remediation_intelligence.get("cached")
    assert len(client.calls) == 4
    with sqlite3.connect(settings.processing_dir.parent / "alt_text_cache.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM descriptions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_response_format_fallback_does_not_store_under_initial_inputs(tmp_path, settings, monkeypatch):
    from app.services import intelligence_llm_utils

    monkeypatch.setattr(intelligence_llm_utils, "FORMAT_CAPABILITIES", intelligence_llm_utils.FormatCapabilityCache())
    class FormatClient(FakeLlmClient):
        base_url = "https://gateway.test/v1"

        async def chat_completion(self, messages, **kwargs):
            if kwargs.get("response_format", {}).get("type") == "json_schema":
                self.calls.append(messages)
                import httpx
                response = httpx.Response(400, request=httpx.Request("POST", self.base_url),
                    json={"error": {"code": "capability_unsupported", "message": "json_schema unsupported"}})
                response.raise_for_status()
            return await super().chat_completion(messages, **kwargs)

    client = FormatClient()
    fig = figure(tmp_path)
    [first] = await alt_text.generate_alt_text([fig], client, settings=settings)
    assert first.generated_text == DESCRIPTION
    assert len(client.calls) == 2
    with sqlite3.connect(settings.processing_dir.parent / "alt_text_cache.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM descriptions").fetchone()[0] == 0
    await alt_text.generate_alt_text([fig], client, settings=settings)
    assert len(client.calls) == 3
    [hit] = await alt_text.generate_alt_text([fig], client, settings=settings)
    assert len(client.calls) == 3
    assert hit.remediation_intelligence["cached"]


@pytest.mark.parametrize("past_boundary", [0, 1])
def test_expired_cache_entry_is_deleted_without_extending_storage_lifetime(tmp_path, monkeypatch, past_boundary):
    from app.services import alt_text_cache

    now = [1_000_000_000_000_000]
    monkeypatch.setattr(alt_text_cache.time, "time_ns", lambda: now[0])
    cache = AltTextCache(tmp_path / "cache.sqlite3")
    stored_at = now[0]
    cache.put("image", "model", "v1", "input", DESCRIPTION)
    now[0] += MAX_AGE_NS - 1
    assert cache.get("image", "model", "v1", "input") == DESCRIPTION
    with sqlite3.connect(cache.path) as connection:
        assert connection.execute("SELECT stored_at, accessed FROM descriptions").fetchone() == (stored_at, now[0])
    now[0] += 1 + past_boundary
    assert cache.get("image", "model", "v1", "input") is None
    with sqlite3.connect(cache.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM descriptions").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["open", "get", "put"])
def test_cache_prunes_expired_rows_on_open_get_and_put(tmp_path, monkeypatch, operation):
    from app.services import alt_text_cache

    now = [1_000_000_000_000_000]
    monkeypatch.setattr(alt_text_cache.time, "time_ns", lambda: now[0])
    cache = AltTextCache(tmp_path / "cache.sqlite3")
    cache.put("expired-image", "model", "v1", "input", "Expired description")
    now[0] += MAX_AGE_NS + 1
    if operation == "open":
        cache._connect().close()
    elif operation == "get":
        assert cache.get("another-image", "model", "v1", "input") is None
    else:
        cache.put("new-image", "model", "v1", "input", "New description")
    with sqlite3.connect(cache.path) as connection:
        rows = connection.execute("SELECT image_hash, stored_at FROM descriptions").fetchall()
    assert rows == ([("new-image", now[0])] if operation == "put" else [])


def test_cache_discards_legacy_rows_without_storage_time(tmp_path):
    path = tmp_path / "cache.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE descriptions (image_hash TEXT, model TEXT, prompt_version TEXT, input_hash TEXT, description TEXT, accessed INTEGER)")
        connection.execute("INSERT INTO descriptions VALUES ('image', 'model', 'v1', 'input', 'Unknown storage time', 1)")
    cache = AltTextCache(path)
    assert cache.get("image", "model", "v1", "input") is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM descriptions").fetchone()[0] == 0
        assert "stored_at" in {row[1] for row in connection.execute("PRAGMA table_info(descriptions)")}


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_file", [False, True])
async def test_cache_disabled_never_opens_file_and_every_call_reaches_model(tmp_path, settings, monkeypatch, existing_file):
    fig = figure(tmp_path, caption="Meaningful figure")
    client = FakeLlmClient()
    cache_path = settings.processing_dir.parent / "alt_text_cache.sqlite3"
    assert settings.alt_text_cache_enabled is True
    if existing_file:
        await alt_text.generate_alt_text([fig], client, settings=settings)
        original_bytes = cache_path.read_bytes()
    before = len(client.calls)
    monkeypatch.setenv("ALT_TEXT_CACHE_ENABLED", "false")
    disabled_settings = Settings(_env_file=None, **settings.model_dump(exclude={"alt_text_cache_enabled"}))
    assert disabled_settings.alt_text_cache_enabled is False
    def no_cache_access(*args, **kwargs):
        raise AssertionError("Disabled caching must not construct or access a cache")
    monkeypatch.setattr(alt_text, "AltTextCache", no_cache_access)
    for _ in range(3):
        [result] = await alt_text.generate_alt_text([fig], client, settings=disabled_settings)
        assert result.generated_text == DESCRIPTION
        assert not result.remediation_intelligence.get("cached")
    assert len(client.calls) == before + 3
    if existing_file:
        assert cache_path.read_bytes() == original_bytes
    else:
        assert not cache_path.exists()


def test_expired_description_text_is_removed_from_database_pages(tmp_path, monkeypatch):
    from app.services import alt_text_cache

    now = [1_000_000_000_000_000]
    monkeypatch.setattr(alt_text_cache.time, "time_ns", lambda: now[0])
    cache = AltTextCache(tmp_path / "cache.sqlite3")
    cache.put("expired-image", "model", "v1", "input", "PRIVATE EXPIRED DESCRIPTION")
    now[0] += MAX_AGE_NS // 2
    cache.put("live-image", "model", "v1", "input", "Still live description")
    now[0] += MAX_AGE_NS // 2
    assert cache.get("expired-image", "model", "v1", "input") is None
    assert cache.get("live-image", "model", "v1", "input") == "Still live description"
    assert b"PRIVATE EXPIRED DESCRIPTION" not in cache.path.read_bytes()
