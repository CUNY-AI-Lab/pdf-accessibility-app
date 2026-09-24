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
    options = parser.parse_args()
    sys.stdout.write(screen_reader_text(options.pdf) + "\n")


if __name__ == "__main__":
    main()
