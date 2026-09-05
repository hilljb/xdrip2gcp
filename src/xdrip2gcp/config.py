"""Configuration loading for xdrip2gcp.

Settings come from three layers, each overriding the one before it:

1. `resources/config.toml` - shared defaults, committed to the repo.
2. `resources/config.local.toml` - per-machine overrides, git-ignored.
3. `XDRIP2GCP_*` environment variables.

Two values are generated rather than configured, and both are persisted to the
local config file so that later runs resolve to the same thing: the bucket name
suffix (bucket names are globally unique) and the Nightscout password. Writing
them down is what makes repeated runs idempotent instead of creating a new
bucket or invalidating the deployed secret every time.
"""

from __future__ import annotations

import dataclasses
import os
import re
import secrets
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
class ServiceAccountConfig:
    """Dedicated identities for building and running the functions."""

    runtime_id: str
    build_id: str
    runtime_role_bucket: str
    build_role_project: str
    bq_runtime_id: str = "xdrip2gcp-bq-runtime"
    bq_role_id: str = "xdrip2gcpBigQueryWriter"


@dataclass(frozen=True)
class FunctionConfig:
    """Cloud Function deployment settings."""

    name: str
    region: str
    runtime: str
    entry_point: str
    source_dir: str
    memory: str
    timeout_seconds: int
    max_instances: int
    allow_unauthenticated: bool

    @property
    def source_path(self) -> Path:
        return REPO_ROOT / self.source_dir


@dataclass(frozen=True)
class BigQueryConfig:
    """Where readings land in BigQuery, and in which timezone they are stamped."""

    location: str
    dataset: str
    entries_table: str
    current_view: str
    latest_table: str
    latest_rows: int
    timezone: str


@dataclass(frozen=True)
class AuthConfig:
    """Nightscout credential settings and scrypt work factors."""

    header_name: str
    secret_id: str
    password: str
    password_bytes: int
    scrypt_n: int
    scrypt_r: int
    scrypt_p: int
    scrypt_dklen: int
    salt_bytes: int
    max_request_bytes: int


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
    services: tuple[str, ...] = ()
    service_accounts: ServiceAccountConfig | None = None
    function: FunctionConfig | None = None
    function_bq: FunctionConfig | None = None
    auth: AuthConfig | None = None
    bigquery: BigQueryConfig | None = None

    @property
    def bucket_uri(self) -> str:
        return f"gs://{self.bucket_name}"

    def test_object_uri(self, name: str) -> str:
        return f"{self.bucket_uri}/{self.test_prefix}/{name}"

    def region_of(self, function: FunctionConfig) -> str:
        """Where a function is deployed, defaulting to the bucket's location."""
        return function.region or self.location

    @property
    def function_region(self) -> str:
        if self.function is None:
            raise ConfigError("[function] is missing from the configuration")
        return self.region_of(self.function)

    @property
    def function_bq_region(self) -> str:
        if self.function_bq is None:
            raise ConfigError("[function_bq] is missing from the configuration")
        return self.region_of(self.function_bq)

    def service_account_email(self, account_id: str) -> str:
        return f"{account_id}@{self.project_id}.iam.gserviceaccount.com"

    @property
    def runtime_service_account(self) -> str:
        if self.service_accounts is None:
            raise ConfigError("[service_accounts] is missing from the configuration")
        return self.service_account_email(self.service_accounts.runtime_id)

    @property
    def build_service_account(self) -> str:
        if self.service_accounts is None:
            raise ConfigError("[service_accounts] is missing from the configuration")
        return self.service_account_email(self.service_accounts.build_id)

    @property
    def bq_runtime_service_account(self) -> str:
        if self.service_accounts is None:
            raise ConfigError("[service_accounts] is missing from the configuration")
        return self.service_account_email(self.service_accounts.bq_runtime_id)

    @property
    def bq_role_name(self) -> str:
        """Full resource name of the custom BigQuery role."""
        if self.service_accounts is None:
            raise ConfigError("[service_accounts] is missing from the configuration")
        return f"projects/{self.project_id}/roles/{self.service_accounts.bq_role_id}"

    @property
    def secret_resource(self) -> str:
        if self.auth is None:
            raise ConfigError("[auth] is missing from the configuration")
        return f"projects/{self.project_id}/secrets/{self.auth.secret_id}"

    @property
    def bigquery_location(self) -> str:
        """The dataset's location, defaulting to the bucket's."""
        if self.bigquery is None:
            raise ConfigError("[bigquery] is missing from the configuration")
        return self.bigquery.location or self.location

    @property
    def dataset_id(self) -> str:
        if self.bigquery is None:
            raise ConfigError("[bigquery] is missing from the configuration")
        return f"{self.project_id}.{self.bigquery.dataset}"

    def table_id(self, table: str) -> str:
        """A fully qualified `project.dataset.table` reference."""
        return f"{self.dataset_id}.{table}"

    def quoted_table(self, table: str) -> str:
        """A table reference ready to drop into SQL."""
        return f"`{self.table_id(table)}`"

    @property
    def entries_table_id(self) -> str:
        if self.bigquery is None:
            raise ConfigError("[bigquery] is missing from the configuration")
        return self.table_id(self.bigquery.entries_table)

    @property
    def latest_table_id(self) -> str:
        if self.bigquery is None:
            raise ConfigError("[bigquery] is missing from the configuration")
        return self.table_id(self.bigquery.latest_table)

    @property
    def current_view_id(self) -> str:
        if self.bigquery is None:
            raise ConfigError("[bigquery] is missing from the configuration")
        return self.table_id(self.bigquery.current_view)


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


LOCAL_CONFIG_HEADER = (
    "# xdrip2gcp local configuration (git-ignored).\n"
    "# Overrides resources/config.toml. Machine- and account-specific values live here.\n"
)


def _section_span(text: str, section: str) -> tuple[int, int] | None:
    """Locate a TOML table's body, as (start, end) offsets into `text`."""
    header = re.search(rf"^[ \t]*\[{re.escape(section)}\][ \t]*$", text, flags=re.MULTILINE)
    if header is None:
        return None
    next_header = re.search(r"^[ \t]*\[", text[header.end() :], flags=re.MULTILINE)
    end = header.end() + next_header.start() if next_header else len(text)
    return header.end(), end


def _persist_local_setting(path: Path, section: str, key: str, value: str) -> None:
    """Record a generated value in the local config file.

    Edits by insertion rather than rewriting the file, so comments and any
    hand-written settings survive, and never touches a key that is already
    present in the target table.
    """
    if not path.exists():
        path.write_text(f"{LOCAL_CONFIG_HEADER}\n[{section}]\n{key} = {_quote_toml(value)}\n")
        return

    text = path.read_text()
    if not text.endswith("\n"):
        text += "\n"

    span = _section_span(text, section)
    if span is None:
        path.write_text(f"{text}\n[{section}]\n{key} = {_quote_toml(value)}\n")
        return

    start, end = span
    if re.search(rf"^[ \t]*{re.escape(key)}[ \t]*=", text[start:end], flags=re.MULTILINE):
        return

    line = f"{key} = {_quote_toml(value)}\n"
    path.write_text(text[:start] + "\n" + line + text[start:].lstrip("\n"))


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
    allow_generation: bool = True,
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
    gcp = raw.get("gcp", {})
    accounts = raw.get("service_accounts", {})
    function = raw.get("function", {})
    function_bq = raw.get("function_bq", {})
    auth = raw.get("auth", {})
    bigquery = raw.get("bigquery", {})

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
            if not allow_generation:
                raise ConfigError(
                    "no bucket name suffix recorded; run src/create_bucket.py to generate one"
                )
            suffix = secrets.token_hex(SUFFIX_BYTES)
            _persist_local_setting(local_path, "bucket", "name_suffix", suffix)
        bucket_name = f"{base_name}-{suffix}"

    validate_bucket_name(bucket_name)

    location = str(bucket.get("location", "")).strip()
    if not location:
        raise ConfigError("[bucket].location is required")

    lifecycle_age_days = int(bucket.get("lifecycle_age_days", 0))
    if lifecycle_age_days < 0:
        raise ConfigError("[bucket].lifecycle_age_days may not be negative")

    latest_rows = int(bigquery.get("latest_rows", 2))
    if latest_rows < 1:
        raise ConfigError("[bigquery].latest_rows must be at least 1")

    timezone_name = str(bigquery.get("timezone", "America/Denver")).strip()
    if not timezone_name:
        raise ConfigError("[bigquery].timezone is required")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigError(
            f"[bigquery].timezone must be an IANA zone name like 'America/Denver', got {timezone_name!r}"
        ) from error

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
        services=tuple(str(name) for name in gcp.get("services", ())),
        service_accounts=ServiceAccountConfig(
            runtime_id=str(accounts.get("runtime_id", "xdrip2gcp-fn-runtime")),
            build_id=str(accounts.get("build_id", "xdrip2gcp-fn-build")),
            runtime_role_bucket=str(accounts.get("runtime_role_bucket", "roles/storage.objectCreator")),
            build_role_project=str(accounts.get("build_role_project", "roles/cloudbuild.builds.builder")),
            bq_runtime_id=str(accounts.get("bq_runtime_id", "xdrip2gcp-bq-runtime")),
            bq_role_id=str(accounts.get("bq_role_id", "xdrip2gcpBigQueryWriter")),
        ),
        function=FunctionConfig(
            name=str(function.get("name", "xdrip2gcp-nightscout-test")),
            region=str(function.get("region", "")).strip(),
            runtime=str(function.get("runtime", "python314")),
            entry_point=str(function.get("entry_point", "nightscout")),
            source_dir=str(function.get("source_dir", "src/functions/nightscout")),
            memory=str(function.get("memory", "256Mi")),
            timeout_seconds=int(function.get("timeout_seconds", 60)),
            max_instances=int(function.get("max_instances", 3)),
            allow_unauthenticated=bool(function.get("allow_unauthenticated", True)),
        ),
        function_bq=FunctionConfig(
            name=str(function_bq.get("name", "xdrip2gcp-nightscout-bq")),
            region=str(function_bq.get("region", "")).strip(),
            runtime=str(function_bq.get("runtime", "python314")),
            entry_point=str(function_bq.get("entry_point", "nightscout_bq")),
            source_dir=str(function_bq.get("source_dir", "src/functions/nightscout_bq")),
            memory=str(function_bq.get("memory", "256Mi")),
            timeout_seconds=int(function_bq.get("timeout_seconds", 60)),
            max_instances=int(function_bq.get("max_instances", 3)),
            allow_unauthenticated=bool(function_bq.get("allow_unauthenticated", True)),
        ),
        bigquery=BigQueryConfig(
            location=str(bigquery.get("location", "")).strip(),
            dataset=str(bigquery.get("dataset", "cgm")).strip(),
            entries_table=str(bigquery.get("entries_table", "entries")).strip(),
            current_view=str(bigquery.get("current_view", "entries_current")).strip(),
            latest_table=str(bigquery.get("latest_table", "entries_latest")).strip(),
            latest_rows=latest_rows,
            timezone=timezone_name,
        ),
        auth=AuthConfig(
            header_name=str(auth.get("header_name", "api-secret")),
            secret_id=str(auth.get("secret_id", "xdrip2gcp-nightscout-api-secret")),
            password=str(auth.get("password", "")).strip(),
            password_bytes=int(auth.get("password_bytes", 24)),
            scrypt_n=int(auth.get("scrypt_n", 16384)),
            scrypt_r=int(auth.get("scrypt_r", 8)),
            scrypt_p=int(auth.get("scrypt_p", 1)),
            scrypt_dklen=int(auth.get("scrypt_dklen", 32)),
            salt_bytes=int(auth.get("salt_bytes", 16)),
            max_request_bytes=int(auth.get("max_request_bytes", 1048576)),
        ),
    )


def ensure_password(config: Config, local_path: Path = LOCAL_CONFIG_PATH) -> tuple[Config, bool]:
    """Return a config that has a Nightscout password, generating one if needed.

    Generation is deliberately not part of `load_config`: the password is only
    needed when deploying or authenticating, and a generated one must be
    recorded before the deployed secret depends on it. The alphabet is URL-safe
    so the password can go straight into xDrip's `https://password@host/api/v1/`
    setting without escaping.
    """
    if config.auth is None:
        raise ConfigError("[auth] is missing from the configuration")
    if config.auth.password:
        return config, False

    password = secrets.token_urlsafe(config.auth.password_bytes)
    _persist_local_setting(local_path, "auth", "password", password)
    return dataclasses.replace(config, auth=dataclasses.replace(config.auth, password=password)), True
