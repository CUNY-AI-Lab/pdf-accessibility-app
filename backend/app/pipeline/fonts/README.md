# Font remediation

The orchestrator calls these passes and serializes `FontRemediation.to_dict()`
when it builds validation output. The stored payload keeps its twenty existing
keys, with `selected_preprocessed_path` included only when a preprocessing lane
was selected. Font functions retain their former messages, stats, and logger
name (`app.pipeline.orchestrator`).

`resources._iter_pdf_fonts()` follows page resources, Form XObjects (including
streams), annotation appearances, and AcroForm resources. It suppresses repeated
indirect resources and appearances. Each pass still decides which fonts to
count once: CID dictionary/width repair tracks descendants; diagnostics,
embedding, and ToUnicode repair track parent fonts.

The old walkers had differences that are preserved in named policies:

- `DEFAULT_RESOURCES`: feature inspection, CID repairs, and embedding. Field
  identity uses raw `objgen`, including the shared `(0, 0)` of direct fields.
  Errors reading a Form's subtype are skipped.
- `DIAGNOSTIC_RESOURCES`: only indirect fields share identity; Form subtype
  errors reach the pass's existing error handling.
- `TOUNICODE_RESOURCES`: indirect field identity, with dictionary-only fields
  and AcroForms.
- `ZERO_BYTE_RESOURCES`: resource fallback for Forms and appearances; visits
  only top-level fields, as the original candidate discovery did.
- `TAGGER_RESOURCES`: accepts mapping-like font dictionaries, starts annotation
  traversal at the `/N`, `/R`, and `/D` children, and omits AcroForm fields.
  The tagger supplies its own object-key helper to retain its debug logging.

The orchestrator re-exports its former font functions and constants for existing
imports. A re-export does not forward monkeypatches: patch `encoding`, `programs`,
`embedding`, `repair`, `diagnostics`, or `lanes` where the tested function looks
up its dependency. Orchestration-level patches still belong in `orchestrator`.
