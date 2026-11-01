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

### Symbol correspondences

`symbol_mappings.json` contains character and glyph-name correspondences, not
font programs. Wingdings / Wingdings 2 / Wingdings 3 character positions follow
Unicode WG2 [N4384, section 7](https://www.unicode.org/wg2/docs/n4384.pdf)
(2012-11-06). Their PostScript names were joined to those standard positions
from the Microsoft Wingdings TrueType cmaps supplied with macOS. This permits
recognition of re-encoded subsets such as PDF byte 33 -> `square4` -> U+25AA,
which must not be interpreted as the standard Wingdings byte 33 (a pencil).
No installed symbol font is needed at runtime.

Symbol and ZapfDingbats byte positions and Adobe glyph names follow Unicode's
[Adobe Symbol](https://www.unicode.org/Public/MAPPINGS/VENDORS/ADOBE/symbol.txt)
and [Zapf Dingbats](https://www.unicode.org/Public/MAPPINGS/VENDORS/ADOBE/zdingbat.txt)
tables (2011-07-12). Modern non-private Unicode glyph-name aliases come from
the corresponding macOS font Unicode cmaps. Private-use-only entries are
excluded. Existing valid ToUnicode entries take precedence over these tables;
unknown glyphs in re-encoded subsets remain unresolved.

Unicode table notice (applies to the derived Symbol/Zapf correspondence data):

Copyright (c) 1991-2011 Unicode, Inc. All Rights reserved.

This file is provided as-is by Unicode, Inc. (The Unicode Consortium). No
claims are made as to fitness for any particular purpose. No warranties of
any kind are expressed or implied. The recipient agrees to determine
applicability of information provided. If this file has been provided on
magnetic media by Unicode, Inc., the sole remedy for any claim will be
exchange of defective media within 90 days of receipt.

Unicode, Inc. hereby grants the right to freely use the information
supplied in this file in the creation of products supporting the
Unicode Standard, and to make copies of this file in any form for
internal or external distribution as long as this notice remains
attached.

### TeX glyph names

`tex_glyph_names.json` maps glyph names from the Computer Modern and AMS fonts
that are not in the Adobe Glyph List (for example `negationslash`,
`summationdisplay`, `parenleftbig`) to Unicode. `_glyph_name_to_unicode`
consults it after the AGL, with and without a `.suffix`. It was generated once
(2026-11-01) from three sources, which share no names:

- 282 names from hayro's `hayro-font/src/generated/tex_glyph_names.rs`
  ([LaurenzV/hayro](https://github.com/LaurenzV/hayro) commit `ba9d3fe`, added
  in PR #1515). hayro built the table from the glyph names in the AMS font
  metrics and mapped them with the Unicode Character Database.
- 116 names from the "TeX-specific glyph names" block of pdf.js's
  `src/core/glyphlist.js` ([mozilla/pdf.js](https://github.com/mozilla/pdf.js)
  commit `6669a99`): the CMEX sized delimiters and operators.
- Four names neither covers, mapped from Unicode Character Database character
  names: `epsilon1` -> U+03F5 GREEK LUNATE EPSILON SYMBOL (in the AMS CMMI
  metrics, code 15, TeX's lunate `\epsilon`, is `epsilon1`; code 34,
  `\varepsilon`, is `epsilon`, which the AGL maps to U+03B5);
  `angbracketleft` -> U+27E8 MATHEMATICAL LEFT ANGLE BRACKET;
  `angbracketright` -> U+27E9 MATHEMATICAL RIGHT ANGLE BRACKET; `mapsto` ->
  U+21A6 RIGHTWARDS ARROW FROM BAR.

Nine pdf.js values are corrected locally against the Unicode Character
Database:

- `intercal` -> U+22BA INTERCALATE. pdf.js has U+1D40 MODIFIER LETTER CAPITAL
  T.
- The sized angle brackets `angbracketleftbig`, `angbracketleftBig`,
  `angbracketleftbigg` and `angbracketleftBigg` -> U+27E8, and the four
  `angbracketright` sizes -> U+27E9, the same characters as the unsized
  names. pdf.js has U+2329 and U+232A. Unicode marks those two Deprecated
  (UCD 18.0.0 `PropList.txt`), and Unicode Technical Report #25, revision 15,
  section 2.10 ("Deprecated Delimiters") says they "are now deprecated for use
  with mathematics because their canonical equivalence to CJK angle brackets
  is likely to result in unintended wide spacing problems when used in
  mathematical formulae", and that U+27E8 and U+27E9 should be used instead.
  Under NFC they become the CJK brackets U+3008 and U+3009.

Some TeX glyphs are parts of a symbol. `negationslash` (U+0338) is set before
the symbol it negates, and CMSY's zero-width `mapsto` bar is set before an
arrow, so extracted text has U+0338 before `=` for `\neq`, and U+21A6 followed
by U+2192 for `\mapsto`.

hayro: Copyright (c) The Hayro Authors, licensed under the Apache License,
Version 2.0 or the MIT license; used here under the Apache License, Version
2.0. pdf.js: Copyright 2012 Mozilla Foundation, licensed under the Apache
License, Version 2.0. The license text is at
http://www.apache.org/licenses/LICENSE-2.0. Both tables were converted to JSON.
hayro's pairs are unchanged; pdf.js's are unchanged except for the corrections
above.

### Program width repair

For simple TrueType fonts, rule 7.21.5 is handled in the dictionary-repair
lane by changing the embedded program's `hmtx` advances to the nearest font
unit for the PDF's existing `/Widths`. PDF encodings select glyphs; ToUnicode
is never used for this selection. Outlines, side bearings, PDF widths and
content operators remain unchanged. Conflicting aliases (one glyph assigned
two PDF widths) are counted and left unresolved. A shared descriptor/program
is copied for the font use being repaired. Local TrueType embedding performs
the same alignment before installing the program.
