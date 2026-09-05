"""Access to the deployed function's source from local code.

The function's `nightscout_core` module is stdlib-only and holds the credential
hash format and request-handling rules. Local scripts and tests import it from
the function's source directory rather than keeping a second copy, so the
deploy path cannot drift from what the function actually enforces.
"""

from __future__ import annotations

import hashlib
import importlib
import shutil
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from .config import REPO_ROOT

DEFAULT_SOURCE_DIR = REPO_ROOT / "src" / "functions" / "nightscout"
BQ_SOURCE_DIR = REPO_ROOT / "src" / "functions" / "nightscout_bq"
CORE_MODULE_NAME = "nightscout_core"
BQ_CORE_MODULE_NAME = "bq_core"

# The BigQuery function imports the Stage 3 credential and routing module
# rather than carrying its own copy, so there is one definition of the auth
# scheme. Its source directory therefore is not what gets deployed: the shared
# module is copied alongside it first, by `staged`.
SHARED_MODULES = (CORE_MODULE_NAME + ".py",)

# Files that exist only locally and must not influence the source hash, or a
# stale cache would trigger a needless redeploy.
IGNORED_NAMES = {"__pycache__", ".DS_Store"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def core_module(source_dir: Path = DEFAULT_SOURCE_DIR) -> ModuleType:
    """Import (once) and return the function's core module."""
    if str(source_dir) not in sys.path:
        sys.path.insert(0, str(source_dir))
    return importlib.import_module(CORE_MODULE_NAME)


def bq_core_module(source_dir: Path = BQ_SOURCE_DIR) -> ModuleType:
    """Import (once) and return the BigQuery function's core module.

    It imports `nightscout_core`, so that module's directory has to be
    importable first; the deployed function gets the same arrangement by
    having the file copied in beside it.
    """
    core_module()
    if str(source_dir) not in sys.path:
        sys.path.insert(0, str(source_dir))
    return importlib.import_module(BQ_CORE_MODULE_NAME)


def shared_module_paths(names: Sequence[str] = SHARED_MODULES) -> list[Path]:
    return [DEFAULT_SOURCE_DIR / name for name in names]


@contextmanager
def staged(source_dir: Path, shared: Sequence[Path] = ()) -> Iterator[Path]:
    """Yield a directory holding a function's source plus any shared modules.

    Deploying from a copy is what lets two functions share a module without
    either vendoring it in the repo: the tree that goes up is complete, while
    the tree that is committed has no duplicate to drift.
    """
    if not shared:
        yield source_dir
        return

    with tempfile.TemporaryDirectory(prefix="xdrip2gcp-source-") as workdir:
        target = Path(workdir) / source_dir.name
        shutil.copytree(
            source_dir,
            target,
            ignore=shutil.ignore_patterns(*IGNORED_NAMES, "*.pyc", "*.pyo"),
        )
        for path in shared:
            shutil.copy2(path, target / path.name)
        yield target


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


def source_digest(source_dir: Path = DEFAULT_SOURCE_DIR, extra_files: Sequence[Path] = ()) -> str:
    """Hash the function's source tree, names and contents together.

    `extra_files` covers modules that are copied in at deploy time: without
    them a change to the shared credential module would leave the BigQuery
    function running the old copy, because its own directory looks unchanged.
    """
    digest = hashlib.sha256()
    for path in source_files(source_dir):
        digest.update(str(path.relative_to(source_dir)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    for path in extra_files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
