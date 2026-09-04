"""Test suite for xdrip2gcp.

Run from the repo root:

    python -m unittest discover -s test -t . -v

Importing this package puts `src/` on `sys.path`, so tests can import the
`xdrip2gcp` package without the project being pip-installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
