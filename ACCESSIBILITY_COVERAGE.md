# Accessibility Coverage

What this tool addresses, what it partially addresses, and what falls outside its scope. Organized by PDF/UA-1 (ISO 14289-1) requirement areas.

---

## Fully addressed

### Document structure tags

The tagger writes a complete PDF/UA structure tree using pikepdf:

- **Headings** (H1–H6) with level normalization based on font-size heuristics
- **Paragraphs** (`<P>`)
- **Tables** with `<Table>`, `<TR>`, `<TH>`, `<TD>`, `/Scope` attributes, `/ID` on header cells, and `/Headers` on data cells for cross-referencing — derived from Docling's header detection
- **Lists** (`<List>`, `<LI>`) with nested list support — child lists attach to their parent `<LI>` element
- **Figures** (`<Figure>`) with attached `/Alt` text
- **Links** (`<Link>`) tagged as struct elements via Object Reference (OBJR) in the ParentTree, with `/Contents` inferred from the link URI or named destination
- **Form fields** (`<Form>`) for widget annotations, tagged via OBJR
- **Other annotations** (`<Annot>`) for non-link, non-widget annotations, with subtype-specific `/Contents` (e.g., "Highlighted text", "File attachment: report.pdf", "Insertion mark")
- **Footnotes** (`<Note>`), **code blocks** (`<Code>`), **formulas** (`<Formula>`) — when detected by Docling
- **Table of contents** (`<TOC>`) structures for detected TOC sections
- **Marked content** — all page content wrapped in BDC/EMC operators
- **ParentTree** NumberTree for annotation-to-structure mapping

### Metadata

- **Document title** set from extracted structure or filename (in `/Title` and XMP)
- **Document language** set via `/Lang` entry; per-element `/Lang` on individual struct elements when a paragraph's language differs from the document default (requires lingua-py for detection)
- **PDF/UA identification** via XMP `pdfuaid:part`
- **Bookmarks** (document outline) generated from the heading hierarchy

### Alt text for images

- Vision LLM generates descriptions for each figure (125-word limit per WCAG guidance)
- Decorative images identified by the LLM and marked as artifacts (no structure element)
- Caption text used as fallback when image extraction or LLM fails
- All generated alt text enters a review workflow for human approval

### OCR for scanned documents

- OCRmyPDF adds a searchable text layer to scanned pages
- Configurable language, page rotation, and deskew
- Multiple modes: skip existing text, redo, or force
- OCR rescue path for digital PDFs with broken font encodings

### Font remediation

- Embeds unembedded fonts via Ghostscript (substitution for missing fonts)
- Repairs missing or invalid ToUnicode CMaps using embedded CFF data
- Fixes CID font dictionaries, CIDSet entries, and CIDToGIDMap
- Filters invalid Unicode mappings (null, U+FEFF, U+FFFE)
- LLM-assisted ActualText overrides for unmapped glyphs (with confidence gating)
- Post-flight diagnostics to verify remediation results

### Validation

- Runs veraPDF against PDF/UA-1 (ISO 14289-1)
- Groups violations by rule ID, severity, and description
- Provides fix guidance for 16 known rule categories (headings, tables, annotations, links, fonts, metadata)

### Content preservation

- Text similarity comparison between source and output (sequence matching)
- Length ratio checks to flag significant content loss
- OCR coverage assessment for scanned documents
- Visual ink sampling to detect blank pages
- Configurable thresholds: FAIL below 82% similarity, WARN below 90%

### Review workflow

The tool generates review tasks when automated remediation can't fully resolve an issue:

| Task type | Trigger | Severity |
|---|---|---|
| `font_text_fidelity` | Remaining font/Unicode mapping errors | Blocking or advisory |
| `alt_text` | Machine-generated alt text needing verification | Advisory |
| `table_semantics` | Incomplete table structure detection | Advisory |
| `annotation_description` | Link/annotation descriptions need review | Advisory |
| `content_fidelity` | Text drift or OCR coverage below threshold | Blocking |
| `reading_order` | Weak ordering signals in fidelity check | Advisory |
| `link_text_quality` | Non-descriptive link text ("click here", bare URLs) | Advisory |
| `internal_link_destinations` | Internal links with broken or missing targets | Advisory |

Each task includes relevant metadata (page numbers, font names, similarity ratios) and is marked blocking (must resolve before download) or advisory (recommended but optional).

---

## Partially addressed

### Link descriptions

- **What works**: Creates `/Contents` entries for link annotations. Fidelity checks flag non-descriptive link text ("click here", bare URLs, single characters) and broken internal link destinations as advisory review tasks.
- **Gap**: Does not rewrite non-descriptive link text — only flags it for human review. Does not check whether external link targets are accessible.

### Form field accessibility

- **What works**: Tags widget annotations as `<Form>` struct elements in the structure tree
- **Gap**: Does not add field labels, tooltip text, or description attributes. Does not validate form field grouping, required-field indicators, or input validation messaging.

### Reading order

- **What works**: Spatial bounding-box matching correlates Docling structure with content stream position. Fidelity checks measure whether structural fragments appear in the correct sequence (hit rate and order rate with configurable thresholds).
- **Gap**: This is heuristic matching, not true layout analysis. Complex multi-column layouts, sidebars, or pull quotes may produce false positives or negatives. No graphical reading-order editor for manual correction.

### Footnotes and endnotes

- **What works**: Docling extracts footnotes; the tagger writes `<Note>` elements
- **Gap**: Does not verify or create back-links between footnote references and footnote bodies, which PDF/UA requires for proper traversal.

---

## Not addressed

These areas fall outside the tool's current scope. Documents with significant requirements in these areas will need additional manual remediation.

### Color and contrast

No analysis of color contrast ratios or detection of information conveyed by color alone. WCAG 2.x contrast requirements (4.5:1 for normal text, 3:1 for large text) are not checked.

### Language changes within a document

The tool sets document-level `/Lang` and, when lingua-py is installed, detects per-element language and sets `/Lang` on individual struct elements that differ from the document default. This handles paragraph-level language switches but not inline spans (e.g., a French phrase within an English paragraph).

### Mathematical content

No detection of mathematical expressions, no conversion to MathML, and no specialized alt text generation for equations. Mathematical content gets tagged as generic text or figures.

### Multimedia

No detection of embedded audio or video content. No support for transcripts, captions, or multimedia alternative text.

### Complex table structures

The tool sets `/Headers` and `/ID` on table cells for header-to-data cross-referencing, handles row and column spans, and respects Docling's header detection. Multi-level headers, nested tables, and rotated headers are not handled. Complex tables generate a `table_semantics` review task for manual review.

### Character edge cases

Ligatures (fi, fl), superscript/subscript formatting, combining characters, and diacritics may not be fully resolved by the font remediation pipeline. The confidence-gating threshold is conservative: uncertain mappings go to human review instead of applying potentially incorrect substitutions.

### Content-level accessibility

The tool operates on document structure, not content quality. It does not check for:

- All-caps body text
- Readability or plain-language compliance
- Flashing or animated content
- Adequate text size or line spacing

### Digital signatures

Accessibility remediation modifies the PDF structure, which invalidates existing digital signatures. The tool does not preserve or re-apply signatures.

### PDF/UA-2 (ISO 14289-2)

The tool validates against PDF/UA-1. PDF/UA-2, based on PDF 2.0, introduces additional requirements (namespaces, pronunciation hints, associated files) that are not yet addressed.

---

## Standards reference

| Standard | Status |
|---|---|
| PDF/UA-1 (ISO 14289-1) | Primary validation target |
| PDF/UA-2 (ISO 14289-2) | Not addressed |
| WCAG 2.x | Structural requirements addressed; visual/content requirements not addressed |
| Section 508 | Covered to the extent that PDF/UA-1 compliance satisfies Section 508 PDF requirements |
