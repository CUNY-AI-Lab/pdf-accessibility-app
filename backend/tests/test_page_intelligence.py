from app.services.page_intelligence import (
    collect_grounded_text_candidates,
    repair_text_candidate,
    suspicious_text_signals,
    text_similarity_score,
)


def test_suspicious_text_signals_ignores_greek_statistical_notation():
    text = (
        "A chi-square test showed a significant relation, χ²(16, N = 760) = 26.31, "
        "p = .05."
    )
    assert "mixed scripts in one text block" not in suspicious_text_signals(text)


def test_suspicious_text_signals_ignores_mathematical_latin_greek_notation():
    text = (
        "Distributions differed significantly (𝜒²(2, 𝑁 = 663) = 13.30, 𝑝 = .0013)."
    )
    assert "mixed scripts in one text block" not in suspicious_text_signals(text)


def test_suspicious_text_signals_ignores_decomposed_latin_diacritics():
    text = "Ba ˇ si ´ c, ˇ Z., Banovac, A., Kru ˇ zi ´ c"
    assert "mixed scripts in one text block" not in suspicious_text_signals(text)


def test_suspicious_text_signals_flags_truly_mixed_scripts():
    text = "Internal Revenue සේවය Service"
    assert "mixed scripts in one text block" in suspicious_text_signals(text)


def test_repair_text_candidate_uses_ftfy():
    assert repair_text_candidate("FranÃ§ais") == "Français"


def test_text_similarity_score_tolerates_curly_quote_cleanup():
    score = text_similarity_score("students ' essays", "students’ essays")
    assert score > 0.96


def test_collect_grounded_text_candidates_respects_resolved_text(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    monkeypatch.setattr(
        "app.services.text_grounding.extract_ocr_text_from_bbox",
        lambda *_args, **_kwargs: "ABSTRACT",
    )

    result = collect_grounded_text_candidates(
        pdf_path,
        {
            "elements": [
                {
                    "review_id": "review-1",
                    "type": "heading",
                    "page": 0,
                    "text": "A B S T R A C T",
                    "actual_text": "ABSTRACT",
                    "bbox": {"l": 10, "t": 20, "r": 100, "b": 40},
                }
            ]
        },
    )

    assert result["target_count"] == 0


def test_collect_grounded_text_candidates_skips_pretag_code_resolution(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    monkeypatch.setattr(
        "app.services.text_grounding.extract_ocr_text_from_bbox",
        lambda *_args, **_kwargs: "1 def fetch_url(entry): 2 return entry['download_url']",
    )

    result = collect_grounded_text_candidates(
        pdf_path,
        {
            "elements": [
                {
                    "review_id": "review-1",
                    "type": "code",
                    "page": 0,
                    "text": "1 2 def fetch_url(entry): return entry['download_url']",
                    "actual_text": "1 def fetch_url(entry):\n2     return entry['download_url']",
                    "resolution_source": "pretag_code_llm_inferred",
                    "semantic_blocking": False,
                    "bbox": {"l": 10, "t": 20, "r": 100, "b": 40},
                }
            ]
        },
    )

    assert result["target_count"] == 0
