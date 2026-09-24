"""Score what the app's AI steps decide, against well-tagged gold documents:
the document title, its language, and figure alt text.

For each gold document ``<gold_dir>/<name>.pdf`` (see ``prepare_gold_roundtrip.py``)
and each candidate directory holding ``<name>.tagged.pdf``:

- Title: the candidate's title matches the gold title (case and spacing aside).
- Language: the primary subtag of /Lang matches (``en`` for ``en-US``).
- Alt text: each gold Figure with alt text is paired with the candidate's
  Figure in the same position on the same page. Coverage is the share of gold
  figures whose partner has usable alt text (not empty, not a placeholder;
  the tagger's own test). A judge model compares each usable alt text with the
  gold one and scores 1-5 how much of what the gold text gives a blind reader
  it gives too. Alt text that repeats text a screen reader already hears on
  the page (usually the caption) is counted separately.

Judgments are cached in ``<candidate_dir>/alt_judgments.json`` so re-scoring
does not pay for them again. The judge runs on the CAIL Gateway with the key
in CAIL_API_KEY:

    CAIL_API_KEY=... uv run python scripts/score_semantics.py \\
        --judge-model deepseek-v4-pro-0813 data/eval/olmocr-bench/gold_rt \\
        data/eval/olmocr-bench/bench_data/ai_gemini/gold_rt ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
from collections import defaultdict
from pathlib import Path

import pikepdf

from app.pipeline.language import normalize_lang_tag
from app.pipeline.tagger import _is_weak_figure_alt_text
from app.services.intelligence_llm_utils import extract_message_json
from app.services.llm_client import LlmClient
from app.services.structure_text import _standard_role, screen_reader_text

GATEWAY = "https://tools.ailab.gc.cuny.edu/v1"

JUDGE_PROMPT = """You compare two alternative texts for the same figure in a document: a
reference written by the document's author for readers who cannot see the
figure, and a candidate written by software. Rate how much of what the
reference gives a blind reader the candidate gives them too:
5 - everything essential in the reference (wording may differ; extra accurate detail is fine)
4 - most of it, missing a minor point
3 - part of it; the reader would miss something important
2 - little of it, or it mostly repeats a caption or label without describing the figure
1 - none of it, or it is wrong or misleading
The two may be in different languages; judge the content, not the language.
Return JSON only: {"score": <1-5>, "reason": "<one sentence>"}."""


def normalized(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def document_title(pdf: pikepdf.Pdf) -> str:
    with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
        title = meta.get("dc:title")
    return str(title or pdf.docinfo.get("/Title", "") or "")


def figures_by_page(pdf: pikepdf.Pdf) -> dict[int, list[str]]:
    """Each page's Figure elements, in structure order, as their alt text."""
    page_index = {page.objgen: index for index, page in enumerate(pdf.pages)}
    root = pdf.Root.StructTreeRoot
    role_map = root.get("/RoleMap")
    figures: dict[int, list[str]] = defaultdict(list)

    def page_of(node) -> int | None:
        if isinstance(node, pikepdf.Dictionary):
            if "/Pg" in node:
                return page_index.get(node.Pg.objgen)
            return page_of(node.get("/K"))
        if isinstance(node, pikepdf.Array):
            return next((page for kid in node if (page := page_of(kid)) is not None), None)
        return None

    def walk(node) -> None:
        if isinstance(node, pikepdf.Array):
            for kid in node:
                walk(kid)
        elif isinstance(node, pikepdf.Dictionary) and "/S" in node:
            if _standard_role(node, role_map) == "Figure":
                page = page_of(node)
                if page is not None:
                    figures[page].append(str(node.get("/Alt", "")))
            elif "/K" in node:
                walk(node.K)

    walk(root.get("/K"))
    return figures


def paired_alts(gold: Path, candidate: Path) -> list[tuple[str, str | None]]:
    """(gold alt, candidate alt or None) for each gold figure with alt text,
    paired by page and order on the page."""
    with pikepdf.open(gold) as gold_pdf, pikepdf.open(candidate) as candidate_pdf:
        gold_figures, candidate_figures = figures_by_page(gold_pdf), figures_by_page(candidate_pdf)
    pairs = []
    for page, alts in sorted(gold_figures.items()):
        theirs = candidate_figures.get(page, [])
        for position, alt in enumerate(alts):
            if alt.strip():
                pairs.append((alt, theirs[position] if position < len(theirs) else None))
    return pairs


async def judge(client: LlmClient, reference: str, candidate: str) -> dict:
    answer = await client.chat_completion(
        [
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": f"Reference: {reference}\n\nCandidate: {candidate}"},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return extract_message_json(answer["choices"][0]["message"])


async def score_candidate(gold_dir: Path, candidate_dir: Path, client: LlmClient) -> None:
    cache_path = candidate_dir / "alt_judgments.json"
    cache: dict[str, dict] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    titles, langs, scores = [], [], []
    covered = copies = gold_count = 0
    lines = []
    for gold in sorted(gold_dir.glob("*.pdf")):
        candidate = candidate_dir / f"{gold.stem}.tagged.pdf"
        if not candidate.exists():
            lines.append(f"  {gold.stem:<24}MISSING")
            continue
        with pikepdf.open(gold) as gold_pdf, pikepdf.open(candidate) as candidate_pdf:
            title_ok = normalized(document_title(gold_pdf)) == normalized(
                document_title(candidate_pdf)
            )
            gold_lang = normalize_lang_tag(str(gold_pdf.Root.get("/Lang", ""))) or ""
            their_lang = normalize_lang_tag(str(candidate_pdf.Root.get("/Lang", ""))) or ""
        lang_ok = gold_lang.split("-")[0] == their_lang.split("-")[0]
        titles.append(title_ok)
        langs.append(lang_ok)
        heard = normalized(screen_reader_text(candidate))
        doc_scores = []
        pairs = paired_alts(gold, candidate)
        gold_count += len(pairs)
        for index, (reference, alt) in enumerate(pairs):
            if alt is None or _is_weak_figure_alt_text(alt):
                continue
            covered += 1
            if len(normalized(alt)) >= 20 and normalized(alt) in heard:
                copies += 1
            key = f"{gold.stem}:{index}:{alt}"
            if key not in cache:
                cache[key] = await judge(client, reference, alt)
                cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
            doc_scores.append(int(cache[key].get("score", 1)))
        scores.extend(doc_scores)
        mean = f"{statistics.mean(doc_scores):.2f}" if doc_scores else "-"
        lines.append(
            f"  {gold.stem:<24}title {'ok ' if title_ok else 'NO '} lang {'ok ' if lang_ok else 'NO '}"
            f" alt {len(doc_scores)}/{len(pairs)} judged {mean}"
        )
    judged = f"judge mean {statistics.mean(scores):.2f}" if scores else "nothing judged"
    print(f"\n{candidate_dir}")
    print(
        f"  title {sum(titles)}/{len(titles)}  lang {sum(langs)}/{len(langs)}  "
        f"alt coverage {covered}/{gold_count}  {judged}"
    )
    print(f"  alt repeating visible text: {copies}/{covered}")
    print("\n".join(lines))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("gold", type=Path)
    parser.add_argument("candidates", type=Path, nargs="+")
    options = parser.parse_args()
    client = LlmClient(
        base_url=GATEWAY,
        api_key=os.environ["CAIL_API_KEY"],
        model=options.judge_model,
        timeout=180,
        max_retries=2,
    )
    try:
        for candidate_dir in options.candidates:
            await score_candidate(options.gold, candidate_dir, client)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
