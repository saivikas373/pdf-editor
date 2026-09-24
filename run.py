#!/usr/bin/env python3
"""Start the PDF editor:  python run.py [file.pdf] [--port N] [--no-browser] [--app]"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pdfeditor.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
