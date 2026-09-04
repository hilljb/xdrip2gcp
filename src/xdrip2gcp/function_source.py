"""Access to the deployed function's source from local code.

The function's `nightscout_core` module is stdlib-only and holds the credential
hash format and request-handling rules. Local scripts and tests import it from
the function's source directory rather than keeping a second copy, so the
deploy path cannot drift from what the function actually enforces.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path
from types import ModuleType

from .config import REPO_ROOT

DEFAULT_SOURCE_DIR = REPO_ROOT / "src" / "functions" / "nightscout"
CORE_MODULE_NAME = "nightscout_core"

# Files that exist only locally and must not influence the source hash, or a
# stale cache would trigger a needless redeploy.
IGNORED_NAMES = {"__pycache__", ".DS_Store"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def core_module(source_dir: Path = DEFAULT_SOURCE_DIR) -> ModuleType:
    """Import (once) and return the function's core module."""
    if str(source_dir) not in sys.path:
        sys.path.insert(0, str(source_dir))
    return importlib.import_module(CORE_MODULE_NAME)


def source_files(source_dir: Path = DEFAULT_SOURCE_DIR) -> list[Path]:
    """Every file that gets uploaded, in a stable order."""
    files = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue
        if any(part in IGNORED_NAMES for part in path.relative_to(source_dir).parts):
            continue
        files.append(path)
    return files


def source_digest(source_dir: Path = DEFAULT_SOURCE_DIR) -> str:
    """Hash the function's source tree, names and contents together."""
    digest = hashlib.sha256()
    for path in source_files(source_dir):
        digest.update(str(path.relative_to(source_dir)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
