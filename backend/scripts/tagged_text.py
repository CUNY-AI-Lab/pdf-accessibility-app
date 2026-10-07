"""Print the text a screen reader hears from a tagged PDF.

This is the text our tagged output and Adobe's are scored on; see
``app/services/structure_text.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.services.structure_text import screen_reader_text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument(
        "--markdown", action="store_true", help="headings, list items, and tables as markup"
    )
    parser.add_argument(
        "--figure-text", action="store_true", help="read text inside Figures without alt text"
    )
    options = parser.parse_args()
    text = screen_reader_text(
        options.pdf, figure_text=options.figure_text, markdown=options.markdown
    )
    sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
