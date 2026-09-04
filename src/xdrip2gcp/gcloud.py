"""Thin wrapper around the `gcloud` CLI.

Stage 2 talks to GCP through the CLI rather than a client library, which keeps
the dependency list empty and reuses the credentials from `gcloud auth login`
(those are not Application Default Credentials, so a client library would need
a separate login step).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Sequence

from .config import Config


class GcloudError(Exception):
    """Raised when a gcloud invocation fails."""

    def __init__(self, args: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.args_run = list(args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"`gcloud {' '.join(args)}` exited {returncode}\n"
            f"stdout: {stdout.strip()}\nstderr: {stderr.strip()}"
        )


@dataclass(frozen=True)
class GcloudResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def json(self) -> Any:
        return json.loads(self.stdout)


def is_available() -> bool:
    return shutil.which("gcloud") is not None


def build_env(config: Config) -> dict[str, str]:
    env = dict(os.environ)
    env["CLOUDSDK_PYTHON"] = config.cloudsdk_python
    env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    return env


def run(
    config: Config,
    args: Sequence[str],
    *,
    check: bool = True,
    stdin_bytes: bytes | None = None,
    timeout: int | None = None,
) -> GcloudResult:
    """Run `gcloud <args>` with the project and interpreter already set.

    `timeout` overrides the configured default for the slow operations, like a
    function deployment that has to build a container image.
    """
    command = ["gcloud", *args]
    if not any(arg.startswith("--project") for arg in args):
        command.append(f"--project={config.project_id}")

    completed = subprocess.run(
        command,
        input=stdin_bytes,
        capture_output=True,
        env=build_env(config),
        timeout=timeout or config.gcloud_timeout_seconds,
        check=False,
    )
    result = GcloudResult(
        returncode=completed.returncode,
        stdout=completed.stdout.decode("utf-8", errors="replace"),
        stderr=completed.stderr.decode("utf-8", errors="replace"),
    )
    if check and not result.ok:
        raise GcloudError(command[1:], result.returncode, result.stdout, result.stderr)
    return result


def run_json(config: Config, args: Sequence[str]) -> Any:
    return run(config, [*args, "--format=json"]).json()


def active_account(config: Config) -> str | None:
    """Return the active gcloud account, or None if nothing is authenticated."""
    result = run(
        config,
        ["auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
        check=False,
    )
    if not result.ok:
        return None
    account = result.stdout.strip()
    return account or None


def preflight(config: Config) -> str | None:
    """Return a human-readable reason GCP calls would fail, or None if ready."""
    if not is_available():
        return "the gcloud CLI is not on PATH"
    if active_account(config) is None:
        return "no active gcloud account; run `gcloud auth login`"
    return None
