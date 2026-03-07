# PDF/UA-1 Rule Coverage Matrix

Updated: 2026-03-07

Source rule set: veraPDF PDF/UA-1 validation profile (`106` rules).

Status legend:
- `Covered`: explicit implementation path plus current benchmark evidence.
- `Partial`: some implementation exists, but there are known limits or dependence on upstream extraction.
- `Unproven`: current outputs may pass, but there is no dedicated rule-specific implementation or enough direct evidence yet.
- `Gap`: no meaningful support today.

## Summary

- Total rules assessed: `106`
- Covered: `43`
- Partial: `53`
- Unproven: `10`
- Gap: `0`
- Evidence base: Benchmark evidence: backend/data/benchmarks/corpus_20260307_094934/corpus_report.md; scanned OCR fixture sweep: backend/data/benchmarks/scanned_fixture_corpus_20260307_rerun/workflow.sqlite3

## Highest-Priority Gaps

- None

## Highest-Priority Partial Coverage Areas

- `7.2-2` Natural language in the Outline entries shall be determined
- `7.2-3` Table element may contain only TR, THead, TBody, TFoot and Caption elements
- `7.2-4` TR element should be contained in Table, THead, TBody or TFoot element
- `7.2-5` THead element should be contained in Table element
- `7.2-6` TBody element should be contained in Table element
- `7.2-7` TFoot element should be contained in Table element
- `7.2-8` TH element should be contained in TR element
- `7.2-9` TD element should be contained in TR element
- `7.2-10` TR element may contain only TH and TD elements
- `7.2-11` Table element should contain zero or one THead kid
- `7.2-12` Table element should contain zero or one TFoot kid
- `7.2-13` If Table element contains TFoot kid, Table element should contain one or more TBody kids
- `7.2-14` If Table element contains THead kid, Table element should contain one or more TBody kids
- `7.2-15` A table cell shall not have intersection with other cells
- `7.2-16` Table element may contain a Caption element as its first or last kid

## Highest-Priority Unproven Areas

- `6.1-1` The file header shall consist of "%PDF-1.n" followed by a single EOL marker, where 'n' is a single digit number between 0 (30h) and 7 (37h)
- `7.2-23` Natural language for text in E attribute shall be determined
- `7.2-32` Natural language for text in E attribute in Span Marked Content shall be determined
- `7.16-1` An encrypted conforming file shall contain a P key in its encryption dictionary (ISO 32000-1:2008, 7.6.3.2, Table 21). The 10th bit position of the P key shall be true
- `7.20-1` A conforming file shall not contain any reference XObjects
- `7.21.3.1-1` For any given composite (Type 0) font within a conforming file, the CIDSystemInfo entry in its CIDFont dictionary and its Encoding dictionary shall have the following relationship: - If the Encoding key in the Type 0 font dictionary is Identity-H or Identity-V, any values of Registry, Ordering, and Supplement may be used in the CIDSystemInfo entry of the CIDFont. - Otherwise, the corresponding Registry and Ordering strings in both CIDSystemInfo dictionaries shall be identical, and the value of the Supplement key in the CIDSystemInfo dictionary of the CIDFont shall be less than or equal to the Supplement key in the CIDSystemInfo dictionary of the CMap
- `7.21.3.3-1` All CMaps used within a PDF/UA file, except those listed in ISO 32000-1:2008, 9.7.5.2, Table 118, shall be embedded in that file as described in ISO 32000-1:2008, 9.7.5
- `7.21.3.3-2` For those CMaps that are embedded, the integer value of the WMode entry in the CMap dictionary shall be identical to the WMode value in the embedded CMap stream
- `7.21.3.3-3` A CMap shall not reference any other CMap except those listed in ISO 32000-1:2008, 9.7.5.2, Table 118
- `7.21.4.2-1` If the FontDescriptor dictionary of an embedded Type 1 font contains a CharSet string, then it shall list the character names of all glyphs present in the font program, regardless of whether a glyph in the font is referenced or used by the PDF or not

## Rule Matrix

| Rule | Tags | Status | Implementation | Notes |
|---|---|---|---|---|
| `5-1` | `metadata` | Covered | `backend/app/pipeline/tagger.py` | PDF/UA identification metadata is written explicitly during tagging and has passing corpus evidence. |
| `5-2` | `metadata` | Covered | `backend/app/pipeline/tagger.py` | PDF/UA identification metadata is written explicitly during tagging and has passing corpus evidence. |
| `5-3` | `metadata` | Covered | `backend/app/pipeline/tagger.py` | PDF/UA identification metadata is written explicitly during tagging and has passing corpus evidence. |
| `5-4` | `metadata` | Covered | `backend/app/pipeline/tagger.py` | PDF/UA identification metadata is written explicitly during tagging and has passing corpus evidence. |
| `5-5` | `metadata` | Covered | `backend/app/pipeline/tagger.py` | PDF/UA identification metadata is written explicitly during tagging and has passing corpus evidence. |
| `6.1-1` | `syntax` | Unproven | `backend/app/pipeline/validator.py` | Outputs currently pass the validator, but the app has no dedicated header normalization or repair path for malformed PDF version markers. |
| `6.2-1` | `syntax` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.1-1` | `artifact` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.1-2` | `artifact` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.1-3` | `artifact` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.1-4` | `syntax` | Covered | `backend/app/pipeline/tagger.py` | The tagging pass explicitly sets /MarkInfo /Suspects to false on output PDFs. |
| `7.1-5` | `structure` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.1-6` | `structure` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.1-7` | `structure` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.1-8` | `metadata` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.1-9` | `metadata` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.1-10` | `syntax` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.1-11` | `structure` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.1-12` | `structure` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.2-2` | `lang` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app sets document language and preserves many language-bearing strings, but it does not yet model multilingual spans or language overrides comprehensively. |
| `7.2-3` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-4` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-5` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-6` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-7` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-8` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-9` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-10` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-11` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-12` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-13` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-14` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-15` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-16` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-17` | `structure,list` | Covered | `backend/app/pipeline/tagger.py` | List output is emitted with L/LI/LBody structure by the tagger and validated in corpus runs. |
| `7.2-18` | `structure,list` | Covered | `backend/app/pipeline/tagger.py` | List output is emitted with L/LI/LBody structure by the tagger and validated in corpus runs. |
| `7.2-19` | `structure,list` | Covered | `backend/app/pipeline/tagger.py` | List output is emitted with L/LI/LBody structure by the tagger and validated in corpus runs. |
| `7.2-20` | `structure,list` | Covered | `backend/app/pipeline/tagger.py` | List output is emitted with L/LI/LBody structure by the tagger and validated in corpus runs. |
| `7.2-21` | `structure,lang` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app sets document language and preserves many language-bearing strings, but it does not yet model multilingual spans or language overrides comprehensively. |
| `7.2-22` | `structure,lang` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app sets document language and preserves many language-bearing strings, but it does not yet model multilingual spans or language overrides comprehensively. |
| `7.2-23` | `structure,lang` | Unproven | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/validator.py` | The app does not currently generate explicit E-attribute language structures, and these rules are not directly exercised by the current corpus. |
| `7.2-24` | `lang,annotation,structure` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app sets document language and preserves many language-bearing strings, but it does not yet model multilingual spans or language overrides comprehensively. |
| `7.2-25` | `structure,lang` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app sets document language and preserves many language-bearing strings, but it does not yet model multilingual spans or language overrides comprehensively. |
| `7.2-26` | `structure,toc` | Partial | `backend/app/pipeline/structure.py<br>backend/app/pipeline/tagger.py` | The app now detects obvious table-of-contents runs and emits TOC/TOCI/Caption structure, but detection remains heuristic and is not yet proven across a broad corpus. |
| `7.2-27` | `structure,toc` | Partial | `backend/app/pipeline/structure.py<br>backend/app/pipeline/tagger.py` | The app now detects obvious table-of-contents runs and emits TOC/TOCI/Caption structure, but detection remains heuristic and is not yet proven across a broad corpus. |
| `7.2-28` | `structure,toc` | Partial | `backend/app/pipeline/structure.py<br>backend/app/pipeline/tagger.py` | The app now detects obvious table-of-contents runs and emits TOC/TOCI/Caption structure, but detection remains heuristic and is not yet proven across a broad corpus. |
| `7.2-29` | `lang` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app sets document language and preserves many language-bearing strings, but it does not yet model multilingual spans or language overrides comprehensively. |
| `7.2-30` | `lang,alt-text` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app sets document language and preserves many language-bearing strings, but it does not yet model multilingual spans or language overrides comprehensively. |
| `7.2-31` | `lang,alt-text` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app sets document language and preserves many language-bearing strings, but it does not yet model multilingual spans or language overrides comprehensively. |
| `7.2-32` | `lang,alt-text` | Unproven | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/validator.py` | The app does not currently generate explicit E-attribute language structures, and these rules are not directly exercised by the current corpus. |
| `7.2-33` | `lang,metadata` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app sets document language and preserves many language-bearing strings, but it does not yet model multilingual spans or language overrides comprehensively. |
| `7.2-34` | `lang,text` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app sets document language and preserves many language-bearing strings, but it does not yet model multilingual spans or language overrides comprehensively. |
| `7.2-36` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-37` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-38` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-39` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-40` | `structure,list` | Covered | `backend/app/pipeline/tagger.py` | List output is emitted with L/LI/LBody structure by the tagger and validated in corpus runs. |
| `7.2-41` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-42` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.2-43` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.3-1` | `alt-text,structure,figure` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/orchestrator.py` | The app builds a fresh standard structure tree, metadata, and figure semantics rather than preserving source ambiguity. |
| `7.4.2-1` | `structure,heading` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py` | Heading tags are emitted and level inference exists, but correctness still depends on structural extraction quality and heuristic level recovery. |
| `7.4.4-1` | `structure,heading` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py` | Heading tags are emitted and level inference exists, but correctness still depends on structural extraction quality and heuristic level recovery. |
| `7.4.4-2` | `structure,heading` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py` | Heading tags are emitted and level inference exists, but correctness still depends on structural extraction quality and heuristic level recovery. |
| `7.4.4-3` | `structure,heading` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py` | Heading tags are emitted and level inference exists, but correctness still depends on structural extraction quality and heuristic level recovery. |
| `7.5-1` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.5-2` | `structure,table` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py<br>backend/app/pipeline/fidelity.py` | The app emits real Table/TR/TH/TD structure and TH scope, but table fidelity still depends on Docling extraction and complex header association remains limited. |
| `7.7-1` | `structure,alt-text` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py` | Formula elements are now emitted as Formula tags with Alt text when structure extraction supplies formula text, but overall formula detection and math semantics remain incomplete. |
| `7.9-1` | `structure,note` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py` | Footnotes are normalized into Note elements with stable IDs, but broader note/endnote modeling is still limited by upstream extraction. |
| `7.9-2` | `structure,note` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/structure.py` | Footnotes are normalized into Note elements with stable IDs, but broader note/endnote modeling is still limited by upstream extraction. |
| `7.10-1` | `syntax` | Covered | `backend/app/pipeline/tagger.py` | The tagging pass normalizes optional content configuration dictionaries by ensuring Name is present and removing forbidden AS entries. |
| `7.10-2` | `syntax` | Covered | `backend/app/pipeline/tagger.py` | The tagging pass normalizes optional content configuration dictionaries by ensuring Name is present and removing forbidden AS entries. |
| `7.11-1` | `syntax` | Covered | `backend/app/pipeline/tagger.py` | The tagging pass normalizes embedded file specifications so embedded attachments always carry non-empty F and UF filename keys. |
| `7.15-1` | `syntax` | Covered | `backend/app/pipeline/tagger.py` | The tagging pass strips dynamic XFA packets from AcroForm dictionaries before writing the accessible output PDF. |
| `7.16-1` | `syntax` | Unproven | `backend/app/pipeline/validator.py` | Encrypted PDF permission handling is not an explicit remediation lane today. |
| `7.18.1-1` | `annotation` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/api/review.py` | The app tags links, widgets, and generic annotations, sets Tabs/S, adds annotation descriptions where needed, and prunes incidental TrapNet/PrinterMark annotations from output pages. |
| `7.18.1-2` | `annotation,alt-text` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/api/review.py` | The app tags links, widgets, and generic annotations, sets Tabs/S, adds annotation descriptions where needed, and prunes incidental TrapNet/PrinterMark annotations from output pages. |
| `7.18.1-3` | `annotation,alt-text` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app partially covers this annotation/media family: form widgets are structurally tagged, and media clip data dictionaries now get syntax-critical CT/Alt backfills, but richer semantics still need dedicated review and modeling. |
| `7.18.2-1` | `annotation` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/api/review.py` | The app tags links, widgets, and generic annotations, sets Tabs/S, adds annotation descriptions where needed, and prunes incidental TrapNet/PrinterMark annotations from output pages. |
| `7.18.3-1` | `page` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/api/review.py` | The app tags links, widgets, and generic annotations, sets Tabs/S, adds annotation descriptions where needed, and prunes incidental TrapNet/PrinterMark annotations from output pages. |
| `7.18.4-1` | `annotation` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/api/review.py` | The app tags links, widgets, and generic annotations, sets Tabs/S, adds annotation descriptions where needed, and prunes incidental TrapNet/PrinterMark annotations from output pages. |
| `7.18.4-2` | `structure` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/api/review.py` | The app tags links, widgets, and generic annotations, sets Tabs/S, adds annotation descriptions where needed, and prunes incidental TrapNet/PrinterMark annotations from output pages. |
| `7.18.5-1` | `annotation` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/api/review.py` | The app tags links, widgets, and generic annotations, sets Tabs/S, adds annotation descriptions where needed, and prunes incidental TrapNet/PrinterMark annotations from output pages. |
| `7.18.5-2` | `structure,annotation,alt-text` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/api/review.py` | The app tags links, widgets, and generic annotations, sets Tabs/S, adds annotation descriptions where needed, and prunes incidental TrapNet/PrinterMark annotations from output pages. |
| `7.18.6.2-1` | `syntax` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app partially covers this annotation/media family: form widgets are structurally tagged, and media clip data dictionaries now get syntax-critical CT/Alt backfills, but richer semantics still need dedicated review and modeling. |
| `7.18.6.2-2` | `alt-text` | Partial | `backend/app/pipeline/tagger.py<br>backend/app/pipeline/fidelity.py` | The app partially covers this annotation/media family: form widgets are structurally tagged, and media clip data dictionaries now get syntax-critical CT/Alt backfills, but richer semantics still need dedicated review and modeling. |
| `7.18.8-1` | `annotation` | Covered | `backend/app/pipeline/tagger.py<br>backend/app/api/review.py` | The app tags links, widgets, and generic annotations, sets Tabs/S, adds annotation descriptions where needed, and prunes incidental TrapNet/PrinterMark annotations from output pages. |
| `7.20-1` | `syntax` | Unproven | `backend/app/pipeline/validator.py` | Reference XObjects are not deliberately normalized or removed by the current pipeline. |
| `7.20-2` | `syntax` | Partial | `backend/app/pipeline/orchestrator.py<br>backend/app/services/font_actualtext.py` | The pipeline traverses Form XObjects in several remediation and review paths, but full structure incorporation of all Form XObject content is not yet proven across all inputs. |
| `7.21.3.1-1` | `font` | Unproven | `backend/app/pipeline/orchestrator.py<br>backend/app/pipeline/validator.py` | veraPDF will still catch these font-level requirements, but the app does not yet have a dedicated deterministic remediation lane for this exact rule family. |
| `7.21.3.2-1` | `font` | Covered | `backend/app/pipeline/orchestrator.py<br>backend/app/api/review.py` | The app has explicit automatic and review-assisted remediation lanes for the dominant PDF/UA font mapping and embedding failures, with passing corpus evidence. |
| `7.21.3.3-1` | `font` | Unproven | `backend/app/pipeline/orchestrator.py<br>backend/app/pipeline/validator.py` | veraPDF will still catch these font-level requirements, but the app does not yet have a dedicated deterministic remediation lane for this exact rule family. |
| `7.21.3.3-2` | `font` | Unproven | `backend/app/pipeline/orchestrator.py<br>backend/app/pipeline/validator.py` | veraPDF will still catch these font-level requirements, but the app does not yet have a dedicated deterministic remediation lane for this exact rule family. |
| `7.21.3.3-3` | `font` | Unproven | `backend/app/pipeline/orchestrator.py<br>backend/app/pipeline/validator.py` | veraPDF will still catch these font-level requirements, but the app does not yet have a dedicated deterministic remediation lane for this exact rule family. |
| `7.21.4.1-1` | `font` | Covered | `backend/app/pipeline/orchestrator.py<br>backend/app/api/review.py` | The app has explicit automatic and review-assisted remediation lanes for the dominant PDF/UA font mapping and embedding failures, with passing corpus evidence. |
| `7.21.4.1-2` | `font` | Partial | `backend/app/pipeline/orchestrator.py<br>backend/app/pipeline/fidelity.py` | The app repairs part of this font family today, but the rule is not yet covered by a dedicated end-to-end guarantee for every font subtype and producer pattern. |
| `7.21.4.2-1` | `font` | Unproven | `backend/app/pipeline/orchestrator.py<br>backend/app/pipeline/validator.py` | veraPDF will still catch these font-level requirements, but the app does not yet have a dedicated deterministic remediation lane for this exact rule family. |
| `7.21.4.2-2` | `font` | Covered | `backend/app/pipeline/orchestrator.py<br>backend/app/api/review.py` | The app has explicit automatic and review-assisted remediation lanes for the dominant PDF/UA font mapping and embedding failures, with passing corpus evidence. |
| `7.21.5-1` | `font` | Partial | `backend/app/pipeline/orchestrator.py<br>backend/app/pipeline/fidelity.py` | The app repairs part of this font family today, but the rule is not yet covered by a dedicated end-to-end guarantee for every font subtype and producer pattern. |
| `7.21.6-1` | `font` | Partial | `backend/app/pipeline/orchestrator.py<br>backend/app/pipeline/fidelity.py` | The app repairs part of this font family today, but the rule is not yet covered by a dedicated end-to-end guarantee for every font subtype and producer pattern. |
| `7.21.6-2` | `font` | Partial | `backend/app/pipeline/orchestrator.py<br>backend/app/pipeline/fidelity.py` | The app repairs part of this font family today, but the rule is not yet covered by a dedicated end-to-end guarantee for every font subtype and producer pattern. |
| `7.21.6-3` | `font` | Partial | `backend/app/pipeline/orchestrator.py<br>backend/app/pipeline/fidelity.py` | The app repairs part of this font family today, but the rule is not yet covered by a dedicated end-to-end guarantee for every font subtype and producer pattern. |
| `7.21.6-4` | `font` | Partial | `backend/app/pipeline/orchestrator.py<br>backend/app/pipeline/fidelity.py` | The app repairs part of this font family today, but the rule is not yet covered by a dedicated end-to-end guarantee for every font subtype and producer pattern. |
| `7.21.7-1` | `font` | Covered | `backend/app/pipeline/orchestrator.py<br>backend/app/api/review.py` | The app has explicit automatic and review-assisted remediation lanes for the dominant PDF/UA font mapping and embedding failures, with passing corpus evidence. |
| `7.21.7-2` | `font` | Covered | `backend/app/pipeline/orchestrator.py<br>backend/app/api/review.py` | The app has explicit automatic and review-assisted remediation lanes for the dominant PDF/UA font mapping and embedding failures, with passing corpus evidence. |
| `7.21.8-1` | `font` | Covered | `backend/app/pipeline/orchestrator.py<br>backend/app/api/review.py` | The app has explicit automatic and review-assisted remediation lanes for the dominant PDF/UA font mapping and embedding failures, with passing corpus evidence. |

## Machine-Readable Export

- CSV: `docs/pdfua_rule_coverage_matrix.csv`
