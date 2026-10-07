"""Score what the app's AI steps decide, against well-tagged gold documents:
the document title, its language, and figure alt text.

For each gold document ``<gold_dir>/<name>.pdf`` (see ``prepare_gold_roundtrip.py``)
and each candidate directory holding ``<name>.tagged.pdf``:

- Title: a judge model scores 1-5 how well the candidate's title tells a
  reader which document this is, compared with the gold title. A missing
  title scores 1.
- Language: the primary subtag of /Lang matches (``en`` for ``en-US``).
- Alt text: each gold Figure with alt text is paired with the candidate's
  Figure in the same position on the same page. Coverage is the share of gold
  figures whose partner has usable alt text (not empty, not a placeholder;
  the tagger's own test). The judge compares each usable alt text with the
  gold one and scores 1-5 how much of what the gold text gives a blind reader
  it gives too. Alt text that repeats text a screen reader already hears on
  the page (usually the caption) is counted separately.

Judgments are cached in ``<candidate_dir>/judgments.json`` so re-scoring does
not pay for them again; a judgment that times out, or that the judge is too
busy for, is asked again on the next run. The judge runs on the CAIL Gateway
with the key in CAIL_API_KEY:

    CAIL_API_KEY=... uv run python scripts/score_semantics.py \\
        --judge-model deepseek-v4-pro-0813 data/eval/olmocr-bench/gold_rt \\
        data/eval/olmocr-bench/bench_data/ai_qwen3-vl-235b-a22b/gold_rt ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import httpx
import pikepdf

from app.pipeline.language import normalize_lang_tag
from app.pipeline.tagger import _is_weak_figure_alt_text
from app.services.intelligence_llm_utils import extract_message_json
from app.services.llm_client import LlmClient
from app.services.structure_text import _standard_role, screen_reader_text

GATEWAY = "https://tools.ailab.gc.cuny.edu/v1"
JUDGE_CONCURRENCY = 8
# The judge ran no inference; asking again later is safe.
BUSY_STATUSES = {429, 502, 503, 504}

TITLE_PROMPT = """You compare two titles for the same document: a reference set by the
document's author and a candidate chosen by software. A screen reader announces
the title when the document opens. Rate how well the candidate tells a reader
which document this is, compared with the reference:
5 - names the document as well as the reference (wording, punctuation and case may differ)
4 - names it, missing a minor part such as a subtitle or version
3 - partly names it; a reader could take it for a related document
2 - little of it, or a generic label such as "Report" or "Chapter 5"
1 - a different document, a file name, or a placeholder
The two may be in different languages; judge the content, not the language.
Return JSON only: {"score": <1-5>, "reason": "<one sentence>"}."""

ALT_PROMPT = """You compare two alternative texts for the same figure in a document: a
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


@dataclass
class Judgment:
    """One comparison for the judge; ``key`` names it in the cache."""

    key: str
    prompt: str
    reference: str
    candidate: str


@dataclass
class Document:
    stem: str
    title: Judgment | None  # None when the candidate has no title
    lang_ok: bool
    gold_figures: int
    alts: list[Judgment]
    copies: int


def normalized(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def document_title(pdf: pikepdf.Pdf) -> str:
    with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
        title = meta.get("dc:title")
    return str(title or pdf.docinfo.get("/Title", "") or "").strip()


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


def read_document(gold: Path, candidate: Path) -> Document:
    with pikepdf.open(gold) as gold_pdf, pikepdf.open(candidate) as candidate_pdf:
        gold_title, their_title = document_title(gold_pdf), document_title(candidate_pdf)
        gold_lang = normalize_lang_tag(str(gold_pdf.Root.get("/Lang", ""))) or ""
        their_lang = normalize_lang_tag(str(candidate_pdf.Root.get("/Lang", ""))) or ""
    heard = normalized(screen_reader_text(candidate))
    pairs = paired_alts(gold, candidate)
    alts = [
        Judgment(f"{gold.stem}:{index}:{alt}", ALT_PROMPT, reference, alt)
        for index, (reference, alt) in enumerate(pairs)
        if alt is not None and not _is_weak_figure_alt_text(alt)
    ]
    return Document(
        stem=gold.stem,
        title=Judgment(f"{gold.stem}:title:{their_title}", TITLE_PROMPT, gold_title, their_title)
        if their_title
        else None,
        lang_ok=gold_lang.split("-")[0] == their_lang.split("-")[0],
        gold_figures=len(pairs),
        alts=alts,
        copies=sum(
            1
            for judgment in alts
            if len(normalized(judgment.candidate)) >= 20 and normalized(judgment.candidate) in heard
        ),
    )


async def judge(client: LlmClient, judgment: Judgment) -> dict:
    answer = await client.chat_completion(
        [
            {"role": "system", "content": judgment.prompt},
            {
                "role": "user",
                "content": f"Reference: {judgment.reference}\n\nCandidate: {judgment.candidate}",
            },
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return extract_message_json(answer["choices"][0]["message"])


def mean(scores: list[int]) -> str:
    return f"{statistics.mean(scores):.2f}" if scores else "-"


async def score_candidate(gold_dir: Path, candidate_dir: Path, client: LlmClient) -> None:
    cache_path = candidate_dir / "judgments.json"
    cache: dict[str, dict] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    documents: list[Document | str] = []
    for gold in sorted(gold_dir.glob("*.pdf")):
        candidate = candidate_dir / f"{gold.stem}.tagged.pdf"
        documents.append(read_document(gold, candidate) if candidate.exists() else gold.stem)
    read = [document for document in documents if isinstance(document, Document)]

    # A judgment that times out or finds the judge busy stays out of the
    # cache; the next run asks again.
    limit = asyncio.Semaphore(JUDGE_CONCURRENCY)
    unjudged = 0

    async def judge_into_cache(judgment: Judgment) -> None:
        nonlocal unjudged
        async with limit:
            try:
                cache[judgment.key] = await judge(client, judgment)
            except httpx.TimeoutException:
                unjudged += 1
                return
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in BUSY_STATUSES:
                    raise
                unjudged += 1
                return
            cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False))

    pending = [
        judgment
        for document in read
        for judgment in [document.title, *document.alts]
        if judgment is not None and judgment.key not in cache
    ]
    await asyncio.gather(*(judge_into_cache(judgment) for judgment in pending))

    def score(judgment: Judgment) -> int | None:
        return int(cache[judgment.key].get("score", 1)) if judgment.key in cache else None

    title_scores, alt_scores, lines = [], [], []
    for document in documents:
        if isinstance(document, str):
            lines.append(f"  {document:<24}MISSING")
            continue
        title = score(document.title) if document.title else 1
        if title is not None:
            title_scores.append(title)
        doc_alts = [s for judgment in document.alts if (s := score(judgment)) is not None]
        alt_scores.extend(doc_alts)
        lines.append(
            f"  {document.stem:<24}title {title if title is not None else '-'}"
            f"  lang {'ok' if document.lang_ok else 'NO'}"
            f"  alt {len(document.alts)}/{document.gold_figures} judged {mean(doc_alts)}"
        )
    covered = sum(len(document.alts) for document in read)
    print(f"\n{candidate_dir}")
    print(
        f"  title judged {mean(title_scores)} ({sum(s >= 4 for s in title_scores)}/{len(read)} at 4+)"
        f"  lang {sum(document.lang_ok for document in read)}/{len(read)}"
        f"  alt coverage {covered}/{sum(document.gold_figures for document in read)}"
        f" judged {mean(alt_scores)}"
        + (f"  ({unjudged} not judged yet; run again)" if unjudged else "")
    )
    print(f"  alt repeating visible text: {sum(document.copies for document in read)}/{covered}")
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
