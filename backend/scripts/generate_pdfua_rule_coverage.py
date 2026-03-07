#!/usr/bin/env python3
from __future__ import annotations

import csv
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

PROFILE_URL = "https://raw.githubusercontent.com/veraPDF/veraPDF-validation-profiles/integration/PDF_UA/PDFUA-1.xml"
ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs"
CSV_PATH = DOCS_DIR / "pdfua_rule_coverage_matrix.csv"
MD_PATH = DOCS_DIR / "pdfua_rule_coverage_matrix.md"

STATUS_ORDER = ["covered", "partial", "unproven", "gap"]
STATUS_LABELS = {
    "covered": "Covered",
    "partial": "Partial",
    "unproven": "Unproven",
    "gap": "Gap",
}
EVIDENCE_TEXT = (
    "Benchmark evidence: backend/data/benchmarks/corpus_20260307_094934/corpus_report.md; "
    "scanned OCR fixture sweep: backend/data/benchmarks/scanned_fixture_corpus_20260307_rerun/workflow.sqlite3"
)

LIST_RULES = {"7.2-17", "7.2-18", "7.2-19", "7.2-20", "7.2-40"}
TABLE_RULES = {
    *(f"7.2-{n}" for n in range(3, 17)),
    *(f"7.2-{n}" for n in range(36, 44)),
    "7.5-1",
    "7.5-2",
}
LANG_PARTIAL_RULES = {
    "7.2-2",
    "7.2-21",
    "7.2-22",
    "7.2-24",
    "7.2-25",
    "7.2-29",
    "7.2-30",
    "7.2-31",
    "7.2-33",
    "7.2-34",
}
LANG_UNPROVEN_RULES = {"7.2-23", "7.2-32"}
TOC_PARTIAL_RULES = {"7.2-26", "7.2-27", "7.2-28"}
HEADING_PARTIAL_RULES = {"7.4.2-1", "7.4.4-1", "7.4.4-2", "7.4.4-3"}
ANNOT_COVERED_RULES = {
    "7.18.1-1",
    "7.18.1-2",
    "7.18.2-1",
    "7.18.3-1",
    "7.18.4-1",
    "7.18.4-2",
    "7.18.5-1",
    "7.18.5-2",
    "7.18.8-1",
}
ANNOT_PARTIAL_RULES = {"7.18.1-3", "7.18.6.2-1", "7.18.6.2-2"}
ANNOT_GAP_RULES = set()
FONT_COVERED_RULES = {
    "7.21.3.2-1",
    "7.21.4.1-1",
    "7.21.4.2-2",
    "7.21.7-1",
    "7.21.7-2",
    "7.21.8-1",
}
FONT_PARTIAL_RULES = {
    "7.21.4.1-2",
    "7.21.5-1",
    "7.21.6-1",
    "7.21.6-2",
    "7.21.6-3",
    "7.21.6-4",
}
FONT_UNPROVEN_RULES = {
    "7.21.3.1-1",
    "7.21.3.3-1",
    "7.21.3.3-2",
    "7.21.3.3-3",
    "7.21.4.2-1",
}


def fetch_rules() -> list[dict[str, str]]:
    ns = {"v": "http://www.verapdf.org/ValidationProfile"}
    xml_bytes = urllib.request.urlopen(PROFILE_URL, timeout=30).read()
    root = ET.fromstring(xml_bytes)
    rows: list[dict[str, str]] = []
    for rule in root.findall('.//v:rule', ns):
        rule_id_node = rule.find('v:id', ns)
        if rule_id_node is None:
            continue
        clause = str(rule_id_node.get('clause') or '').strip()
        test = str(rule_id_node.get('testNumber') or '').strip()
        key = f"{clause}-{test}" if clause and test else ""
        rows.append({
            "rule_id": key,
            "clause": clause,
            "test_number": test,
            "object": str(rule.get('object') or '').strip(),
            "tags": str(rule.get('tags') or '').strip(),
            "description": (rule.findtext('v:description', default='', namespaces=ns) or '').strip(),
        })
    return rows


def classify(rule_id: str, tags: str, description: str) -> tuple[str, str, str]:
    if rule_id.startswith('5-'):
        return (
            'covered',
            'PDF/UA identification metadata is written explicitly during tagging and has passing corpus evidence.',
            'backend/app/pipeline/tagger.py',
        )
    if rule_id in {'6.2-1', '7.1-1', '7.1-2', '7.1-3', '7.1-5', '7.1-6', '7.1-7', '7.1-8', '7.1-9', '7.1-10', '7.1-11', '7.1-12', '7.3-1'}:
        return (
            'covered',
            'The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity.',
            'backend/app/pipeline/tagger.py, backend/app/pipeline/orchestrator.py',
        )
    if rule_id == '6.1-1':
        return (
            'unproven',
            'Outputs currently pass the validator, but the app has no dedicated header normalization or repair path for malformed PDF version markers.',
            'backend/app/pipeline/validator.py',
        )
    if rule_id == '7.1-4':
        return (
            'covered',
            'The tagging pass explicitly sets /MarkInfo /Suspects to false on output PDFs.',
            'backend/app/pipeline/tagger.py',
        )
    if rule_id in LIST_RULES:
        return (
            'covered',
            'List output is emitted with L/LI/LBody structure by the tagger and validated in corpus runs.',
            'backend/app/pipeline/tagger.py',
        )
    if rule_id in TABLE_RULES:
        return (
            'partial',
            'The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited.',
            'backend/app/pipeline/tagger.py, backend/app/pipeline/structure.py, backend/app/pipeline/fidelity.py',
        )
    if rule_id in LANG_PARTIAL_RULES:
        return (
            'partial',
            'The app sets document language and preserves many language-bearing strings, but it does not yet model multilingual spans or language overrides comprehensively.',
            'backend/app/pipeline/tagger.py, backend/app/pipeline/fidelity.py',
        )
    if rule_id in LANG_UNPROVEN_RULES:
        return (
            'unproven',
            'The app does not currently generate explicit E-attribute language structures, and these rules are not directly exercised by the current corpus.',
            'backend/app/pipeline/tagger.py, backend/app/pipeline/validator.py',
        )
    if rule_id in TOC_PARTIAL_RULES:
        return (
            'partial',
            'The app now detects obvious table-of-contents runs and emits TOC/TOCI/Caption structure, but detection remains heuristic and is not yet proven across a broad corpus.',
            'backend/app/pipeline/structure.py, backend/app/pipeline/tagger.py',
        )
    if rule_id in HEADING_PARTIAL_RULES:
        return (
            'partial',
            'Heading tags are emitted and level inference exists, but correctness still depends on structural extraction quality and heuristic level recovery.',
            'backend/app/pipeline/tagger.py, backend/app/pipeline/structure.py',
        )
    if rule_id == '7.7-1':
        return (
            'partial',
            'Formula elements are now emitted as Formula tags with Alt text when structure extraction supplies formula text, but overall formula detection and math semantics remain incomplete.',
            'backend/app/pipeline/tagger.py, backend/app/pipeline/structure.py',
        )
    if rule_id in {'7.9-1', '7.9-2'}:
        return (
            'partial',
            'Footnotes are normalized into Note elements with stable IDs, but broader note/endnote modeling is still limited by upstream extraction.',
            'backend/app/pipeline/tagger.py, backend/app/pipeline/structure.py',
        )
    if rule_id in {'7.10-1', '7.10-2'}:
        return (
            'covered',
            'The tagging pass normalizes optional content configuration dictionaries by ensuring Name is present and removing forbidden AS entries.',
            'backend/app/pipeline/tagger.py',
        )
    if rule_id == '7.11-1':
        return (
            'covered',
            'The tagging pass normalizes embedded file specifications so embedded attachments always carry non-empty F and UF filename keys.',
            'backend/app/pipeline/tagger.py',
        )
    if rule_id == '7.15-1':
        return (
            'covered',
            'The tagging pass strips dynamic XFA packets from AcroForm dictionaries before writing the accessible output PDF.',
            'backend/app/pipeline/tagger.py',
        )
    if rule_id == '7.16-1':
        return (
            'unproven',
            'Encrypted PDF permission handling is not an explicit remediation lane today.',
            'backend/app/pipeline/validator.py',
        )
    if rule_id in ANNOT_COVERED_RULES:
        return (
            'covered',
            'The app tags links, widgets, and generic annotations, sets Tabs/S, adds annotation descriptions where needed, and prunes incidental TrapNet/PrinterMark annotations from output pages.',
            'backend/app/pipeline/tagger.py, backend/app/api/review.py',
        )
    if rule_id in ANNOT_PARTIAL_RULES:
        return (
            'partial',
            'The app partially covers this annotation/media family: form widgets are structurally tagged, and media clip data dictionaries now get syntax-critical CT/Alt backfills, but richer semantics still need dedicated review and modeling.',
            'backend/app/pipeline/tagger.py, backend/app/pipeline/fidelity.py',
        )
    if rule_id in ANNOT_GAP_RULES:
        return (
            'gap',
            'This annotation/media subtype is not a dedicated remediation target in the current app.',
            'backend/app/pipeline/validator.py',
        )
    if rule_id == '7.20-1':
        return (
            'unproven',
            'Reference XObjects are not deliberately normalized or removed by the current pipeline.',
            'backend/app/pipeline/validator.py',
        )
    if rule_id == '7.20-2':
        return (
            'partial',
            'The pipeline traverses Form XObjects in several remediation and review paths, but full structure incorporation of all Form XObject content is not yet proven across all inputs.',
            'backend/app/pipeline/orchestrator.py, backend/app/services/font_actualtext.py',
        )
    if rule_id in FONT_COVERED_RULES:
        return (
            'covered',
            'The app has explicit automatic and review-assisted remediation lanes for the dominant PDF/UA font mapping and embedding failures, with passing corpus evidence.',
            'backend/app/pipeline/orchestrator.py, backend/app/api/review.py',
        )
    if rule_id in FONT_PARTIAL_RULES:
        return (
            'partial',
            'The app repairs part of this font family today, but the rule is not yet covered by a dedicated end-to-end guarantee for every font subtype and producer pattern.',
            'backend/app/pipeline/orchestrator.py, backend/app/pipeline/fidelity.py',
        )
    if rule_id in FONT_UNPROVEN_RULES:
        return (
            'unproven',
            'veraPDF will still catch these font-level requirements, but the app does not yet have a dedicated deterministic remediation lane for this exact rule family.',
            'backend/app/pipeline/orchestrator.py, backend/app/pipeline/validator.py',
        )
    if tags == 'metadata':
        return (
            'covered',
            'Metadata output is explicitly rebuilt by the tagger and validated in current corpora.',
            'backend/app/pipeline/tagger.py',
        )
    return (
        'unproven',
        'No dedicated rule-specific remediation path was identified; current confidence comes mainly from validator pass-through rather than explicit implementation coverage.',
        'backend/app/pipeline/validator.py',
    )


def write_outputs(rows: list[dict[str, str]]) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        'rule_id', 'clause', 'test_number', 'object', 'tags', 'status',
        'description', 'rationale', 'implementation_paths', 'evidence',
    ]
    with CSV_PATH.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row['status'] for row in rows)
    gaps = [row for row in rows if row['status'] == 'gap']
    partials = [row for row in rows if row['status'] == 'partial']
    unproven = [row for row in rows if row['status'] == 'unproven']

    def fmt_rule_list(items: list[dict[str, str]], limit: int = 12) -> str:
        return '\n'.join(
            f"- `{row['rule_id']}` {row['description']}"
            for row in items[:limit]
        ) or '- None'

    with MD_PATH.open('w', encoding='utf-8') as f:
        f.write('# PDF/UA-1 Rule Coverage Matrix\n\n')
        f.write('Updated: 2026-03-07\n\n')
        f.write('Source rule set: veraPDF PDF/UA-1 validation profile (`106` rules).\n\n')
        f.write('Status legend:\n')
        f.write('- `Covered`: explicit implementation path plus current benchmark evidence.\n')
        f.write('- `Partial`: some implementation exists, but there are known limits or dependence on upstream extraction.\n')
        f.write('- `Unproven`: current outputs may pass, but there is no dedicated rule-specific implementation or enough direct evidence yet.\n')
        f.write('- `Gap`: no meaningful support today.\n\n')
        f.write('## Summary\n\n')
        f.write(f"- Total rules assessed: `{len(rows)}`\n")
        for status in STATUS_ORDER:
            f.write(f"- {STATUS_LABELS[status]}: `{counts.get(status, 0)}`\n")
        f.write(f"- Evidence base: {EVIDENCE_TEXT}\n\n")
        f.write('## Highest-Priority Gaps\n\n')
        f.write(fmt_rule_list(gaps, limit=15))
        f.write('\n\n## Highest-Priority Partial Coverage Areas\n\n')
        f.write(fmt_rule_list(partials, limit=15))
        f.write('\n\n## Highest-Priority Unproven Areas\n\n')
        f.write(fmt_rule_list(unproven, limit=15))
        f.write('\n\n## Rule Matrix\n\n')
        f.write('| Rule | Tags | Status | Implementation | Notes |\n')
        f.write('|---|---|---|---|---|\n')
        for row in rows:
            impl = row['implementation_paths'].replace(', ', '<br>')
            notes = row['rationale'].replace('|', '\\|')
            f.write(
                f"| `{row['rule_id']}` | `{row['tags']}` | {STATUS_LABELS[row['status']]} | `{impl}` | {notes} |\n"
            )
        f.write('\n## Machine-Readable Export\n\n')
        f.write(f'- CSV: `{CSV_PATH.relative_to(ROOT)}`\n')


def main() -> None:
    rows = fetch_rules()
    output_rows: list[dict[str, str]] = []
    for row in rows:
        status, rationale, implementation_paths = classify(
            row['rule_id'], row['tags'], row['description']
        )
        row['status'] = status
        row['rationale'] = rationale
        row['implementation_paths'] = implementation_paths
        row['evidence'] = EVIDENCE_TEXT
        output_rows.append(row)

    output_rows.sort(key=lambda row: [int(part) if part.isdigit() else part for part in row['rule_id'].replace('.', '-').split('-')])
    write_outputs(output_rows)
    counts = Counter(row['status'] for row in output_rows)
    print('Wrote', MD_PATH)
    print('Wrote', CSV_PATH)
    print(dict(counts))


if __name__ == '__main__':
    main()
