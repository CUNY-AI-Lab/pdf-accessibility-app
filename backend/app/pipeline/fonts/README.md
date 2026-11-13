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

### Glyph names nothing resolves

A Differences name replaces the glyph at its code, so the base encoding's
character at that code is not evidence for it, in any simple font. A name that
neither the AGL nor the TeX table resolves gets Unicode only from other
evidence: an authored ToUnicode entry (the rebuilt ToUnicode keeps it), or,
for a TrueType font, the embedded program's Unicode cmap entry for the glyph of
that name (private-use values excluded). Otherwise the code stays unmapped and
veraPDF reports it (7.21.7-1) and the document is held for review. For
example, `epsilonlunatesymbol` in a WinAnsi-based Type 1 font had been read as
the base encoding's U+001F at its code.

### Substitute programs

Standard-14 and Arial/Times/Courier fonts get Liberation (`fonts-liberation2`)
TrueType programs. A Type 1 font that becomes TrueType, or a TrueType font on
StandardEncoding, gets its glyph names restated over WinAnsiEncoding: PDF/UA-1
7.21.6 allows a non-symbolic TrueType font only MacRomanEncoding or
WinAnsiEncoding as its base, and only AGL names in Differences. Each code keeps
its glyph; codes StandardEncoding leaves undefined take WinAnsiEncoding's
character. Acrobat's form font (`/Helv`, Differences with no base) is the
common case.

Families Liberation lacks are converted from the URW base-35 Type 1 programs
(`fonts-urw-base35`, installed in the image, not in the repository) to
TrueType at runtime (`urw.py`), so their advances can be aligned to the PDF's
widths. Century Gothic gets URW Gothic (owner-approved; not metric
compatible). Arial Narrow and Helvetica Narrow get Nimbus Sans Narrow, which
fontconfig's `conf.d/30-metric-aliases.conf` lists as metric-compatible with
both; Liberation Sans Narrow is not in Liberation 2. An installed original
program is used before any substitute.

### Program width repair

For simple TrueType fonts, rule 7.21.5 is handled in the dictionary-repair
lane by changing the embedded program's `hmtx` advances to the nearest font
unit for the PDF's existing `/Widths`. PDF encodings select glyphs; ToUnicode
is never used for this selection. Outlines, side bearings, PDF widths and
content operators remain unchanged. Conflicting aliases (one glyph assigned
two PDF widths) are counted and left unresolved. A shared descriptor/program
is copied for the font use being repaired. Local TrueType embedding performs
the same alignment before installing the program.

CIDFontType0 fonts with a CID-keyed CFF program (`/CIDFontType0C`) get the
same treatment in that lane: each charstring's advance is restated as the
CIDFont's `/W` width (or `/DW`), scaled by the program's FontMatrix, and `/W`
is not changed. A glyph whose advance comes from a subroutine is
desubroutinized first, as fontTools' `CFFToCFF2` does to drop widths;
outlines and hints are kept. The fonts Acrobat's OCR generates for
recognized text on scanned pages (`*Times New Roman-18786` and the like) give
some glyphs a negative program advance and record 0 in `/W`. veraPDF reads a
program advance of exactly -1 as a missing glyph and compares `/DW` instead,
so copying program advances into `/W` (what the OCR lanes'
`_sync_pdf_cid_cff_widths` does) cannot clear those glyphs.

### Invisible OCR fonts

`invisible.repair_invisible_fonts()` runs in the dictionary-repair lane before
`repair_notdef_content`. It handles a Type0 font with `/Identity-H` that shows
text only in rendering mode 3, such as Acrobat's `HiddenHorzOCR`, when a used
code selects .notdef in its embedded program. Rule 7.21.8 applies in every
rendering mode. The descendant becomes a CIDFontType2 with a TrueType built at
runtime (glyph 0 .notdef, glyph 1 empty) and a `/CIDToGIDMap` that sends CIDs 1
through the highest used code to glyph 1, the structure of Tesseract's
`GlyphLessFont`. CID 0 stays .notdef. The Type0 dictionary, ToUnicode, `/W` and
`/DW` stay, and the content is unchanged. veraPDF's 7.21.5 width check exempts
rendering mode 3, so one advance (`/DW`) serves every width. Pages, nested
Forms, appearances, tiling patterns, Type 3 glyphs and soft masks are walked
with `Tr` tracked through q/Q; a font painted in any of them, or listed in the
AcroForm `/DR`, is left alone. `repair_notdef_content` never removes codes
shown in rendering mode 3: they are the document's recognized text.

### Writing ToUnicode CMaps

`encoding._render_tounicode_cmap()` writes runs of consecutive codes whose
destinations are consecutive single BMP code points as `bfrange` entries, and
everything else as `bfchar`, at most 100 entries per block. A range stays
within one 256-code block on both sides: only the last source byte may vary
(Adobe Technical Note #5014), and the last destination byte must not pass 255
(ISO 32000-1, 9.10.3). `_parse_tounicode_map_details` reads the result back
exactly.

### OCR font mapping (`ocr_unicode.py`)

The `ocr_tounicode` lane runs after the deterministic ToUnicode lane when
7.21.7-1 remains, on digital documents too, because it changes no page content.
It targets used codes of embedded simple and Identity-encoded Type0 fonts that
have no valid ToUnicode entry and no mapping the font itself gives; symbol
fonts are never targeted. Each page that draws such a code is rendered and read
with Tesseract (hOCR words and character boxes). An OCR word with exactly as
many letters as glyphs under it gives each glyph its letter; otherwise the
character nearest the glyph's center votes. Occurrences of one outline in
several subsets of a font vote together. A code maps when at least two readings
agree, with 60% of them and three times the runner-up; quote styles vote
together, and a `|` split with `I` or `l` counts for the letter. A glyph with
no contours, or no ink where OCR read nothing, maps to a space (or U+200B
without an advance). Two shapes of one font reading as the same character keep
only the better-supported one. Confident codes are written even when others
stay unmapped; validation keeps that partial repair, and the document stays held.
