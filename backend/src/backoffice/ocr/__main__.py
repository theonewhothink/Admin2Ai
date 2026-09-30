"""``python -m backoffice.ocr gate PREVIOUS CURRENT [--max-drop X]`` (§56 release gate)."""

import sys

from .benchmark import main

sys.exit(main())
