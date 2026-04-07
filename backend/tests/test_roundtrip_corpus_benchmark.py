import json
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest

import scripts.roundtrip_corpus_benchmark as roundtrip_corpus_benchmark
from scripts.roundtrip_corpus_benchmark import (
    RoundtripCorpusRow,
    discover_manifests,
    resolve_workflow_runner,
    run_roundtrip_case,
    run_roundtrip_case_isolated,
    write_outputs,
)


def _build_gold_pdf(path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 300))

    font = pdf.make_indirect(
        pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Font"),
            "/Subtype": pikepdf.Name("/Type1"),
            "/BaseFont": pikepdf.Name("/Helvetica"),
        })
    )
    page["/Resources"] = pikepdf.Dictionary({
        "/Font": pikepdf.Dictionary({
            "/F1": font,
        })
    })
    page["/Contents"] = pdf.make_stream(
        pikepdf.unparse_content_stream(
            [
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
                pikepdf.ContentStreamInstruction([pikepdf.Name("/F1"), 12], pikepdf.Operator("Tf")),
                pikepdf.ContentStreamInstruction([20, 240], pikepdf.Operator("Td")),
                pikepdf.ContentStreamInstruction([pikepdf.String("Introduction")], pikepdf.Operator("Tj")),
                pikepdf.ContentStreamInstruction([0, -24], pikepdf.Operator("Td")),
                pikepdf.ContentStreamInstruction(
                    [pikepdf.String("This paragraph preserves the intended meaning of the document.")],
                    pikepdf.Operator("Tj"),
                ),
                pikepdf.ContentStreamInstruction([0, -24], pikepdf.Operator("Td")),
                pikepdf.ContentStreamInstruction([pikepdf.String("Open the guide")], pikepdf.Operator("Tj")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
            ]
        )
    )

    link = pdf.make_indirect(
        pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Annot"),
            "/Subtype": pikepdf.Name("/Link"),
            "/Rect": pikepdf.Array([18, 188, 120, 205]),
            "/Border": pikepdf.Array([0, 0, 0]),
            "/Contents": pikepdf.String("Open the guide"),
            "/A": pikepdf.Dictionary({
                "/S": pikepdf.Name("/URI"),
                "/URI": pikepdf.String("https://example.com/guide"),
            }),
        })
    )
    widget = pdf.make_indirect(
        pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Annot"),
            "/Subtype": pikepdf.Name("/Widget"),
            "/FT": pikepdf.Name("/Tx"),
            "/Rect": pikepdf.Array([20, 130, 180, 150]),
            "/T": pikepdf.String("name"),
            "/TU": pikepdf.String("Name"),
            "/V": pikepdf.String("Ada"),
        })
    )
    page["/Annots"] = pikepdf.Array([link, widget])
    page["/Tabs"] = pikepdf.Name("/S")

    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({
        "/Marked": True,
    })
    pdf.Root["/Lang"] = pikepdf.String("en-US")
    pdf.Root["/AcroForm"] = pikepdf.Dictionary({
        "/Fields": pikepdf.Array([widget]),
    })
    pdf.docinfo["/Title"] = pikepdf.String("Accessible Test Document")

    with pdf.open_outline() as outline:
        outline.root.append(pikepdf.OutlineItem("Introduction", 0))

    pdf.save(path)


def _write_manifest(path: Path, gold_pdf: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "id": "synthetic_case",
                "document_family": "synthetic_doc",
                "product_priority": "primary",
                "workflow_tags": ["links", "forms"],
                "gold_pdf": gold_pdf.name,
                "recoverable_assertions": [
                    {"id": "lang", "kind": "document_lang", "expected": "en-US"},
                    {"id": "title", "kind": "title_contains", "expected": "Accessible Test Document"},
                    {
                        "id": "field",
                        "kind": "field_accessible_name",
                        "field_name": "name",
                        "expected": "Name",
                    },
                    {
                        "id": "link",
                        "kind": "link_contents",
                        "page": 1,
                        "expected": "Open the guide",
                    },
                ],
                "hidden_semantics_assertions": [],
            },
            indent=2,
        )
        + "\n"
    )


@pytest.mark.asyncio
async def test_run_roundtrip_case_generates_reports(tmp_path: Path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    manifest_path = tmp_path / "synthetic.roundtrip.json"
    case_dir = tmp_path / "case"
    _build_gold_pdf(gold_pdf)
    _write_manifest(manifest_path, gold_pdf)

    async def _fake_workflow_runner(pdf_path, run_dir, settings, session_maker, job_manager):
        candidate_pdf = run_dir / f"{Path(pdf_path).stem}_workflow_output.pdf"
        candidate_pdf.write_bytes(gold_pdf.read_bytes())
        (run_dir / f"{Path(pdf_path).stem}_workflow_structure.json").write_text(
            json.dumps(
                {
                    "elements": [
                        {"type": "heading", "text": "Introduction"},
                        {
                            "type": "paragraph",
                            "text": "This paragraph preserves the intended meaning of the document.",
                        },
                        {"type": "paragraph", "text": "Open the guide"},
                    ]
                },
                indent=2,
            )
            + "\n"
        )
        return SimpleNamespace(
            final_status="complete",
            compliant=True,
            fidelity_passed=True,
            error="",
        )

    async def _fake_load_or_extract_gold_structure(*, case_id, gold_pdf):
        assert case_id == "synthetic_case"
        assert gold_pdf.name == "gold.pdf"
        return {
            "elements": [
                {"type": "heading", "text": "Introduction"},
                {
                    "type": "paragraph",
                    "text": "This paragraph preserves the intended meaning of the document.",
                },
                {"type": "paragraph", "text": "Open the guide"},
            ]
        }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        roundtrip_corpus_benchmark,
        "_load_or_extract_gold_structure",
        _fake_load_or_extract_gold_structure,
    )

    row = await run_roundtrip_case(
        manifest_path=manifest_path,
        case_dir=case_dir,
        settings=object(),
        session_maker=None,
        job_manager=SimpleNamespace(),
        workflow_runner=_fake_workflow_runner,
    )

    assert row.case_id == "synthetic_case"
    assert row.document_family == "synthetic_doc"
    assert row.product_priority == "primary"
    assert row.workflow_tags == "links,forms"
    assert row.recoverable_passed == 4
    assert row.recoverable_failed == 0
    assert row.document_lang_match is True
    assert row.title_match is True
    assert (case_dir / "strip_summary.json").exists()
    assert (case_dir / "roundtrip_compare.json").exists()
    assert (case_dir / "roundtrip_compare.md").exists()
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_run_roundtrip_case_isolated_creates_case_local_workflow_db(tmp_path: Path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    manifest_path = tmp_path / "synthetic.roundtrip.json"
    case_dir = tmp_path / "case"
    _build_gold_pdf(gold_pdf)
    _write_manifest(manifest_path, gold_pdf)

    async def _fake_workflow_runner(pdf_path, run_dir, settings, session_maker=None, job_manager=None):
        assert session_maker is not None
        assert job_manager is not None
        candidate_pdf = run_dir / f"{Path(pdf_path).stem}_workflow_output.pdf"
        candidate_pdf.write_bytes(gold_pdf.read_bytes())
        (run_dir / f"{Path(pdf_path).stem}_workflow_structure.json").write_text(
            json.dumps(
                {
                    "elements": [
                        {"type": "heading", "text": "Introduction"},
                        {
                            "type": "paragraph",
                            "text": "This paragraph preserves the intended meaning of the document.",
                        },
                        {"type": "paragraph", "text": "Open the guide"},
                    ]
                },
                indent=2,
            )
            + "\n"
        )
        return SimpleNamespace(
            final_status="complete",
            compliant=True,
            fidelity_passed=True,
            error="",
        )

    async def _fake_load_or_extract_gold_structure(*, case_id, gold_pdf):
        return {
            "elements": [
                {"type": "heading", "text": "Introduction"},
                {
                    "type": "paragraph",
                    "text": "This paragraph preserves the intended meaning of the document.",
                },
                {"type": "paragraph", "text": "Open the guide"},
            ]
        }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        roundtrip_corpus_benchmark,
        "_load_or_extract_gold_structure",
        _fake_load_or_extract_gold_structure,
    )

    row = await run_roundtrip_case_isolated(
        manifest_path=manifest_path,
        case_dir=case_dir,
        settings=object(),
        workflow_runner=_fake_workflow_runner,
    )

    assert row.case_id == "synthetic_case"
    assert (case_dir / "workflow_benchmark.sqlite3").exists()
    monkeypatch.undo()


def test_discover_manifests_and_write_outputs(tmp_path: Path) -> None:
    first = tmp_path / "a.roundtrip.json"
    second_dir = tmp_path / "nested"
    second_dir.mkdir()
    second = second_dir / "b.roundtrip.json"
    first.write_text("{}\n")
    second.write_text("{}\n")

    manifests = discover_manifests(roots=[tmp_path], explicit=[])
    assert manifests == [first.resolve(), second.resolve()]

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    rows = [
        RoundtripCorpusRow(
            case_id="a",
            document_family="manual",
            product_priority="primary",
            workflow_tags="toc,bookmarks",
            manifest_path=str(first),
            gold_pdf="gold.pdf",
            stripped_pdf="stripped.pdf",
            candidate_pdf="candidate.pdf",
            workflow_final_status="complete",
            workflow_compliant=True,
            workflow_fidelity_passed=True,
            recoverable_total=4,
            recoverable_passed=4,
            recoverable_failed=0,
            recoverable_invalid=0,
            hidden_total=0,
            hidden_passed=0,
            hidden_failed=0,
            hidden_invalid=0,
            raw_text_similarity=1.0,
            structure_transcript_similarity=1.0,
            reading_order_hit_rate=1.0,
            reading_order_order_rate=1.0,
            field_match_rate=1.0,
            link_match_rate=1.0,
            bookmark_match_rate=1.0,
            recoverable_bookmark_match_rate=1.0,
            higher_order_bookmark_match_rate=1.0,
            page_count_match=True,
            document_lang_match=True,
            title_match=True,
            error="",
        )
    ]

    write_outputs(out_dir, rows)

    assert (out_dir / "roundtrip_corpus_summary.csv").exists()
    assert (out_dir / "roundtrip_corpus_summary.json").exists()
    assert (out_dir / "roundtrip_corpus_report.md").exists()
    report = (out_dir / "roundtrip_corpus_report.md").read_text()
    assert "## Universal Invariants" in report
    assert "## Family Coverage" in report
    assert "manual: cases=1" in report
    assert "avg_recoverable_bookmark_match=1.0" in report


def test_resolve_workflow_runner_returns_callable() -> None:
    runner = resolve_workflow_runner("assistive-core")
    assert callable(runner)
