# old_print: sources

49 single-page, image-only PDFs from 11 public-domain printed books (1794–1925), with 509 tests in `old_print.jsonl` (289 present, 147 order, 73 absent).

## Books

| # | Book | Year | Internet Archive identifier | Gutenberg ebook | Pages used (printed page → IA `n` index) | Typeface and condition | Public-domain basis |
|---|------|------|-----------------------------|-----------------|------------------------------------------|------------------------|---------------------|
| 1 | Elizabeth Fulhame, *An Essay on Combustion, with a View to a New Art of Dying and Painting* (London: printed for the author by J. Cooper) | 1794 | `bim_eighteenth-century_an-essay-on-combustion-_fulhame-mrs_1794` | 77630 | 45 → n60; 150 → n165 | Caslon-style type with long s (ſ) and ct ligatures, catchwords, spaced semicolons and colons; bitonal microfilm scan with black borders, 800 ppi. The only long-s book: every test uses words without a long s. | Published 1794 |
| 2 | John Dalton, *A New System of Chemical Philosophy*, Part First of Vol. II (Manchester: Executors of S. Russell for George Wilson) | 1827 | `newsystemofchemi0002john` | 74948 | 18 → n33; 38 → n53; 91 → n106; 217 → n232; 274 → n289; 303 → n318 | Early 19th-century book: heavy modern-face type, uneven inking, footnotes marked with * † ‡, small ruled tables, vulgar fractions, displayed formulae with italic variables; greyish paper | Published 1827 |
| 3 | James F. W. Johnston, *Elements of Agricultural Chemistry and Geology* (New York: Wiley and Putnam) | 1842 | `elementsofagricu00john` (Library of Congress) | 73427 | 30 → n35; 95 → n100; 200 → n205; 240 → n245 | Heavily foxed and browned paper, a brown stain ring over the text on p. 200, show-through; spaced semicolons; bold small-caps running heads | Published 1842 |
| 4 | Alice Cary, *Snow-Berries: A Book for Young Folks* (Boston: Ticknor and Fields) | 1867 | `snowberries00caryrich` | 79573 | 12 → n27; 45 → n60; 78 → n95; 120 → n141 (verse); 190 → n215 | Strongly yellowed paper, light inking, show-through from the verso, spaced semicolons and exclamation marks | Published 1867 |
| 5 | Henry Green, *Shakespeare and the Emblem Writers* (London: Trübner) | 1870 | `shakemblem00greeuoft` (University of Toronto) | 50006 | 63 → n104; 65 → n106; 66 → n107; 242 → n289 | Grey, low-contrast scan with slight skew; oldstyle numerals; italic running heads and long italic runs; blackletter titles set inline in the body (the blackletter words themselves are not tested) | Published 1870 |
| 6 | G. K. Gilbert, *Report on the Geology of the Henry Mountains* (Washington: Government Printing Office) | 1877 | `reportongeologyo00gilb` (BYU) | 75119 | 4 → n21; 29 → n60; 77 → n134; 100 → n157 | Technical report: dot-leader stratigraphic list with hanging indents and italic Latin species names (p. 4), ruled table of specific gravities (p. 77), wood-engraved diagrams with small captions and text wrapping beside a figure (p. 29), italic section heading (p. 100); library accession stamp on p. 29 | Published 1877; also a U.S. federal government work |
| 7 | M. E. Braddon, *Rupert Godwin: A Novel* (London: Simpkin, Marshall, Hamilton, Kent) | 1890 | `rupertgodwinnove00brad` | 77631 | 24 → n31; 93 → n100; 150 → n157; 212 → n219; 277 → n284 | Cheap novel in small, dense type with broken sorts and patchy, faint inking; italic running heads; chapter openings with small caps | Published 1890 |
| 8 | *The Encyclopaedia Britannica*, 11th ed., Vol. VI (New York: Encyclopaedia Britannica) | 1910 (IA copy dated c1910–1922) | `encyclopaediabrit06chisrich` | 31447 (pp. 262, 305, 373); 31641 (pp. 431, 460) | 262 → n289; 305 → n332; 373 → n400; 431 → n458; 460 → n487 | Two-column small print; bold italic side-notes in the margins (pp. 305, 460); footnotes in smaller type; bibliographies in smaller type with French italics; Greek | Published 1910 |
| 9 | *Some Imagist Poets, 1917: An Annual Anthology* (Boston: Houghton Mifflin) | 1917 | `someimagistpoets00aldi` (Boston Public Library) | 79529 | 7 → n20; 27 → n40; 47 → n60; 57 → n70 | Free verse on grey, low-contrast paper; small-caps openings; italic stanzas; thin spaces before ; and ?; bracketed page numbers at the foot | Published 1917 |
| 10 | Henry A. Wallace, *Agricultural Prices* (Des Moines: Wallace Publishing) | 1920 | `agriculturalpric00wall` | 76047 | 15 → n18; 30 → n33; 46 → n49; 81 → n84; 125 → n128 | Faint grey type; chapter openings with drop caps; dot-leader tables, a ruled table with rotated column heads (p. 46), dense numeric tables (p. 125); red-ink handwritten correction on p. 81 (not tested) | Published 1920 (before 1929) |
| 11 | Fanny Heaslip Lea, *The Dream-Maker Man* (New York: Dodd, Mead) | 1925 | `dreammakerman0000fann` | 79331 | 12 → n21; 51 → n60; 102 → n111; 180 → n189; 240 → n249 | Novel on browned paper with water-stain tide lines; spaced ellipses, many em dashes, oldstyle folio numerals | Published 1925 (before 1929) |

Page URLs have the form `https://archive.org/details/<identifier>/page/n<N>/mode/1up`. Each PDF was made from `https://archive.org/download/<identifier>/page/n<N>.jpg`.

All eleven books were published before 1929, so they are in the public domain in the United States. The Internet Archive scans are faithful reproductions of public-domain works. The Gutenberg texts are public-domain transcriptions, and each test quotes 8–25 words from them.

## PDFs

- `bench_data/pdfs/old_print/<id>.pdf`: one page each. The IA page JPEG is embedded byte for byte with `img2pdf` at the item's scan resolution (the `ppi` field in its IA metadata; 300 ppi for the Green item, which records none). Spot checks against the items' JP2 files confirmed that the page JPEGs are full resolution.
- Every PDF has no text layer: `pdftotext` returns nothing and `pdffonts` lists no fonts.

## Ground truth method

- Passages come from the Project Gutenberg HTML text for the same edition. All eleven Gutenberg ebooks were proofread from page images of that edition (Internet Archive images, per their credits, except the EB slices, whose credits name no source).
- Every passage was checked by eye against the page image at readable zoom. Where the scan and Gutenberg differ, the test uses the scan's wording:
  - Rupert Godwin: p. 150 "half a guinea" (Gutenberg: "half-a-guinea"); p. 212 "waistcoat pocket" and "rock-work" (Gutenberg: "waistcoat-pocket", "rockwork"). Gutenberg's transcriber regularized hyphens.
  - Dalton: p. 303 "may be" (Gutenberg: "maybe"). The scan's misprints "vaponr" and "eonsidered" were avoided.
  - Wallace: p. 46 table row "Mill feeds" (Gutenberg: "Mill-feeds").
- Passages avoid line-end hyphens where possible. A few order anchors start mid-word after a line-end hyphen (for example "vate the old pastures." for "reno-/vate"), which matches whether or not an engine joins the word.
- Trailing semicolons, colons, question marks and exclamation marks were dropped because several books space them off from the word. Passages whose inner punctuation is spaced were rewritten to avoid it.
- Gutenberg places page markers at slightly different points than the printed page breaks for Gilbert (figures moved), Dalton (footnotes moved) and a few page-final lines. In each case the image was treated as authoritative.
- `max_diffs` is 0 for passages up to 80 characters, 1 for 81–120 characters and 2 for longer passages.
- Absent tests cover printed page numbers (`first_n: 40`, or `last_n: 40` for Imagist folios at the foot) and running heads, all case-insensitive. Each running head was checked not to appear in that page's body text. The p. 29 Gilbert page number has no absent test because the accession stamp "157429" contains it.
- Following the `old_scans.jsonl` schema, order tests carry no `case_sensitive` field.
- No OCR engine or vision model was used to create or check the passages. IA's `_page_numbers.json` files, which IA derives from its OCR, were used only to find which image index holds a given printed page. The printed page number was then read from the image.
