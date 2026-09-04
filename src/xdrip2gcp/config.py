"""Configuration loading for xdrip2gcp.

Settings come from three layers, each overriding the one before it:

1. `resources/config.toml` - shared defaults, committed to the repo.
2. `resources/config.local.toml` - per-machine overrides, git-ignored.
3. `XDRIP2GCP_*` environment variables.

The bucket name gets special treatment. Cloud Storage bucket names are globally
unique, so the name is `<base_name>-<name_suffix>` with a suffix generated once
and persisted to the local config file. That keeps runs idempotent: the first
run picks a name, every later run resolves to the same one.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_CONFIG_PATH = REPO_ROOT / "resources" / "config.toml"
LOCAL_CONFIG_PATH = REPO_ROOT / "resources" / "config.local.toml"

SUFFIX_BYTES = 3

# Environment variable -> (section, key) in the config tables.
ENV_OVERRIDES = {
    "XDRIP2GCP_PROJECT_ID": ("project", "id"),
    "XDRIP2GCP_BUCKET_NAME": ("bucket", "name"),
    "XDRIP2GCP_BUCKET_BASE_NAME": ("bucket", "base_name"),
    "XDRIP2GCP_BUCKET_NAME_SUFFIX": ("bucket", "name_suffix"),
    "XDRIP2GCP_LOCATION": ("bucket", "location"),
    "XDRIP2GCP_CLOUDSDK_PYTHON": ("gcloud", "cloudsdk_python"),
}

MIN_GCLOUD_PYTHON = (3, 10)


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Resolved, validated configuration."""

    project_id: str
    bucket_name: str
    location: str
    storage_class: str
    uniform_bucket_level_access: bool
    public_access_prevention: bool
    lifecycle_age_days: int
    test_prefix: str
    data_prefix: str
    test_data: dict[str, Any] = field(default_factory=dict)
    cloudsdk_python: str = ""
    gcloud_timeout_seconds: int = 120

    @property
    def bucket_uri(self) -> str:
        return f"gs://{self.bucket_name}"

    def test_object_uri(self, name: str) -> str:
        return f"{self.bucket_uri}/{self.test_prefix}/{name}"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    for env_name, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value:
            raw.setdefault(section, {})[key] = value
    return raw


def _quote_toml(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _persist_bucket_suffix(suffix: str, path: Path) -> None:
    """Record a generated bucket suffix so later runs reuse the same bucket.

    Only ever appends to a `[bucket]` table, and only when no suffix is
    recorded yet, so hand-written values in the local config are left alone.
    """
    existing = path.read_text() if path.exists() else ""
    if re.search(r"^\s*name_suffix\s*=", existing, flags=re.MULTILINE):
        return

    if not existing:
        header = (
            "# xdrip2gcp local configuration (git-ignored).\n"
            "# Overrides resources/config.toml. Machine- and account-specific values live here.\n\n"
        )
        body = f"[bucket]\nname_suffix = {_quote_toml(suffix)}\n"
        path.write_text(header + body)
        return

    text = existing if existing.endswith("\n") else existing + "\n"
    if re.search(r"^\s*\[bucket\]\s*$", text, flags=re.MULTILINE):
        text = re.sub(
            r"^(\s*\[bucket\]\s*)$",
            lambda match: f"{match.group(1)}\nname_suffix = {_quote_toml(suffix)}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        text += f"\n[bucket]\nname_suffix = {_quote_toml(suffix)}\n"
    path.write_text(text)


def validate_bucket_name(name: str) -> None:
    """Check a name against Cloud Storage's bucket naming rules.

    Underscores and dots are legal in bucket names but rejected here: dots
    require domain verification, and underscores break DNS-style addressing.
    """
    if not 3 <= len(name) <= 63:
        raise ConfigError(f"bucket name must be 3-63 characters, got {len(name)}: {name!r}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", name):
        raise ConfigError(
            f"bucket name {name!r} must be lowercase letters, digits and dashes, "
            "starting and ending with a letter or digit"
        )
    if name.startswith("goog"):
        raise ConfigError(f"bucket name {name!r} may not start with 'goog'")
    if "google" in name:
        raise ConfigError(f"bucket name {name!r} may not contain 'google'")


def resolve_cloudsdk_python(configured: str) -> str:
    """Find a Python 3.10+ interpreter for the gcloud CLI to run under.

    Stage 1 notes that gcloud fails on an older system Python, so every
    invocation gets CLOUDSDK_PYTHON set explicitly rather than relying on the
    caller's shell.
    """
    if configured:
        path = Path(configured).expanduser()
        if not path.exists():
            raise ConfigError(f"configured cloudsdk_python does not exist: {path}")
        return str(path)

    if sys.version_info[:2] >= MIN_GCLOUD_PYTHON:
        return sys.executable

    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        candidate = Path(conda_prefix) / "bin" / "python"
        if candidate.exists():
            return str(candidate)

    raise ConfigError(
        "could not find a Python 3.10+ interpreter for gcloud; set "
        "[gcloud].cloudsdk_python in resources/config.local.toml"
    )


def load_config(
    shared_path: Path = SHARED_CONFIG_PATH,
    local_path: Path = LOCAL_CONFIG_PATH,
    *,
    allow_suffix_generation: bool = True,
) -> Config:
    """Load, merge and validate configuration into a `Config`."""
    if not shared_path.exists():
        raise ConfigError(f"missing shared config file: {shared_path}")

    raw = _deep_merge(_read_toml(shared_path), _read_toml(local_path))
    raw = _apply_env_overrides(raw)

    project = raw.get("project", {})
    bucket = raw.get("bucket", {})
    objects = raw.get("objects", {})
    test_data = raw.get("test_data", {})
    gcloud_settings = raw.get("gcloud", {})

    project_id = str(project.get("id", "")).strip()
    if not project_id:
        raise ConfigError("[project].id is required")

    bucket_name = str(bucket.get("name", "")).strip()
    if not bucket_name:
        base_name = str(bucket.get("base_name", "")).strip()
        if not base_name:
            raise ConfigError("[bucket].base_name is required when [bucket].name is empty")
        suffix = str(bucket.get("name_suffix", "")).strip()
        if not suffix:
            if not allow_suffix_generation:
                raise ConfigError(
                    "no bucket name suffix recorded; run src/create_bucket.py to generate one"
                )
            suffix = secrets.token_hex(SUFFIX_BYTES)
            _persist_bucket_suffix(suffix, local_path)
        bucket_name = f"{base_name}-{suffix}"

    validate_bucket_name(bucket_name)

    location = str(bucket.get("location", "")).strip()
    if not location:
        raise ConfigError("[bucket].location is required")

    lifecycle_age_days = int(bucket.get("lifecycle_age_days", 0))
    if lifecycle_age_days < 0:
        raise ConfigError("[bucket].lifecycle_age_days may not be negative")

    return Config(
        project_id=project_id,
        bucket_name=bucket_name,
        location=location,
        storage_class=str(bucket.get("storage_class", "STANDARD")).strip().upper(),
        uniform_bucket_level_access=bool(bucket.get("uniform_bucket_level_access", True)),
        public_access_prevention=bool(bucket.get("public_access_prevention", True)),
        lifecycle_age_days=lifecycle_age_days,
        test_prefix=str(objects.get("test_prefix", "test-data")).strip("/"),
        data_prefix=str(objects.get("data_prefix", "cgm-data")).strip("/"),
        test_data=dict(test_data),
        cloudsdk_python=resolve_cloudsdk_python(str(gcloud_settings.get("cloudsdk_python", ""))),
        gcloud_timeout_seconds=int(gcloud_settings.get("timeout_seconds", 120)),
    )
