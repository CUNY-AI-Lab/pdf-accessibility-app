"""Export a final tagged PDF to self-contained HTML (no pipeline/model calls).

From backend: PYTHONPATH=. .venv/bin/python scripts/export_html.py tagged.pdf -o document.html
"""

import argparse
import sys
from pathlib import Path

from app.services.html_export import export_html


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="write UTF-8 HTML here (default: stdout)")
    args = parser.parse_args()
    if args.output and args.output.resolve() == args.pdf.resolve():
        parser.error("output must differ from the input PDF")
    document = export_html(args.pdf)
    if args.output:
        args.output.write_text(document, encoding="utf-8")
    else:
        sys.stdout.write(document)


if __name__ == "__main__":
    main()
