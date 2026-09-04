"""Idempotent Cloud Storage operations driven by the `gcloud` CLI.

Every function here is safe to call repeatedly: creating a bucket that already
exists, applying a lifecycle rule that already matches, uploading bytes that
are already stored, and deleting an object that is already gone are all no-ops
that report what they did rather than failing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from .actions import ActionResult
from .config import Config
from .gcloud import GcloudError, run


class BucketOwnedElsewhereError(Exception):
    """Raised when the bucket name exists but belongs to another project."""


def _object_uri(config: Config, object_path: str) -> str:
    return f"{config.bucket_uri}/{object_path.lstrip('/')}"


def describe_bucket(config: Config) -> dict[str, Any] | None:
    """Return bucket metadata, or None if the bucket does not exist."""
    result = run(config, ["storage", "buckets", "describe", config.bucket_uri, "--format=json"], check=False)
    if result.ok:
        return json.loads(result.stdout)

    stderr = result.stderr
    if "not found" in stderr.lower() or "404" in stderr:
        return None
    if "403" in stderr or "does not have storage.buckets.get" in stderr:
        raise BucketOwnedElsewhereError(
            f"{config.bucket_uri} exists but is not accessible from project "
            f"{config.project_id}. Bucket names are globally unique; pick another "
            f"by changing [bucket].name_suffix in resources/config.local.toml.\n{stderr.strip()}"
        )
    raise GcloudError(["storage", "buckets", "describe"], result.returncode, result.stdout, stderr)


def bucket_exists(config: Config) -> bool:
    return describe_bucket(config) is not None


def create_bucket(config: Config) -> ActionResult:
    """Create the configured bucket if it is not already there."""
    if bucket_exists(config):
        return ActionResult(False, f"bucket {config.bucket_uri} already exists")

    args = [
        "storage",
        "buckets",
        "create",
        config.bucket_uri,
        f"--location={config.location}",
        f"--default-storage-class={config.storage_class}",
    ]
    if config.uniform_bucket_level_access:
        args.append("--uniform-bucket-level-access")
    if config.public_access_prevention:
        args.append("--public-access-prevention")

    result = run(config, args, check=False)
    if result.ok:
        return ActionResult(True, f"created bucket {config.bucket_uri} in {config.location}")

    # A concurrent run may have won the race between our check and our create.
    if "409" in result.stderr or "already own it" in result.stderr:
        return ActionResult(False, f"bucket {config.bucket_uri} already exists")
    raise GcloudError(args, result.returncode, result.stdout, result.stderr)


def _current_lifecycle_ages(metadata: dict[str, Any]) -> list[int]:
    """Extract the ages of existing Delete lifecycle rules."""
    lifecycle = metadata.get("lifecycle_config") or metadata.get("lifecycle") or {}
    rules = lifecycle.get("rule") or lifecycle.get("rules") or []
    ages = []
    for rule in rules:
        action = rule.get("action", {})
        condition = rule.get("condition", {})
        if str(action.get("type", "")).lower() == "delete" and "age" in condition:
            ages.append(int(condition["age"]))
    return sorted(ages)


def apply_lifecycle(config: Config) -> ActionResult:
    """Ensure the bucket deletes objects after `lifecycle_age_days`.

    A lifecycle rule caps what repeated test runs can cost: even if a test
    leaves objects behind, Cloud Storage removes them a few days later.
    """
    metadata = describe_bucket(config)
    if metadata is None:
        raise RuntimeError(f"{config.bucket_uri} does not exist; create it first")

    current = _current_lifecycle_ages(metadata)
    desired = [config.lifecycle_age_days] if config.lifecycle_age_days > 0 else []

    if current == desired:
        described = f"delete after {desired[0]} day(s)" if desired else "no lifecycle rule"
        return ActionResult(False, f"lifecycle already correct ({described})")

    if not desired:
        run(config, ["storage", "buckets", "update", config.bucket_uri, "--clear-lifecycle"])
        return ActionResult(True, "cleared lifecycle rules")

    policy = {"rule": [{"action": {"type": "Delete"}, "condition": {"age": config.lifecycle_age_days}}]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(policy, handle)
        policy_path = handle.name
    try:
        run(
            config,
            ["storage", "buckets", "update", config.bucket_uri, f"--lifecycle-file={policy_path}"],
        )
    finally:
        Path(policy_path).unlink(missing_ok=True)

    return ActionResult(True, f"set lifecycle rule: delete objects after {config.lifecycle_age_days} day(s)")


def ensure_bucket(config: Config) -> list[ActionResult]:
    """Bring the bucket to its configured state, whatever state it starts in."""
    results = [create_bucket(config)]
    results.append(apply_lifecycle(config))
    return results


def describe_object(config: Config, object_path: str) -> dict[str, Any] | None:
    """Return object metadata, or None if the object does not exist."""
    result = run(
        config,
        ["storage", "objects", "describe", _object_uri(config, object_path), "--format=json"],
        check=False,
    )
    if result.ok:
        return json.loads(result.stdout)
    if "not found" in result.stderr.lower() or "404" in result.stderr:
        return None
    raise GcloudError(
        ["storage", "objects", "describe", object_path], result.returncode, result.stdout, result.stderr
    )


def _md5_base64(payload: bytes) -> str:
    return base64.b64encode(hashlib.md5(payload).digest()).decode("ascii")


def upload_bytes(config: Config, object_path: str, payload: bytes) -> ActionResult:
    """Upload bytes, skipping the transfer when identical bytes are stored.

    Cloud Storage returns an MD5 for non-composite objects, so comparing
    against it makes repeated uploads free instead of merely harmless.
    """
    uri = _object_uri(config, object_path)
    existing = describe_object(config, object_path)
    if existing is not None and existing.get("md5_hash") == _md5_base64(payload):
        return ActionResult(False, f"{uri} already up to date")

    suffix = Path(object_path).suffix
    with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as handle:
        handle.write(payload)
        local_path = handle.name
    try:
        run(config, ["storage", "cp", local_path, uri])
    finally:
        Path(local_path).unlink(missing_ok=True)

    verb = "updated" if existing is not None else "uploaded"
    return ActionResult(True, f"{verb} {uri} ({len(payload)} bytes)")


def download_bytes(config: Config, object_path: str) -> bytes:
    """Read an object's contents back out of the bucket."""
    uri = _object_uri(config, object_path)
    with tempfile.TemporaryDirectory() as workdir:
        local_path = Path(workdir) / (Path(object_path).name or "object")
        run(config, ["storage", "cp", uri, str(local_path)])
        return local_path.read_bytes()


def list_objects(config: Config, prefix: str = "") -> list[str]:
    """List object paths under a prefix, relative to the bucket root."""
    uri = config.bucket_uri
    if prefix:
        uri = f"{uri}/{prefix.strip('/')}"
    result = run(config, ["storage", "ls", "--recursive", uri], check=False)
    if not result.ok:
        if "matched no objects" in result.stderr.lower():
            return []
        raise GcloudError(["storage", "ls", uri], result.returncode, result.stdout, result.stderr)

    root = f"{config.bucket_uri}/"
    paths = []
    for line in result.stdout.splitlines():
        line = line.strip()
        # A recursive listing interleaves objects with "<dir>/:" header lines
        # and bare "<dir>/" placeholders; neither is an object.
        if not line.startswith(root) or line.endswith("/") or line.endswith("/:"):
            continue
        paths.append(line[len(root) :])
    return sorted(paths)


def delete_object(config: Config, object_path: str) -> ActionResult:
    """Delete an object, tolerating one that is already gone."""
    uri = _object_uri(config, object_path)
    result = run(config, ["storage", "rm", uri], check=False)
    if result.ok:
        return ActionResult(True, f"deleted {uri}")
    if "not found" in result.stderr.lower() or "404" in result.stderr or "matched no objects" in result.stderr.lower():
        return ActionResult(False, f"{uri} was already absent")
    raise GcloudError(["storage", "rm", uri], result.returncode, result.stdout, result.stderr)


def _flag_enabled(value: Any) -> bool:
    """Normalize a gcloud boolean that may arrive as a bool or a nested table."""
    if isinstance(value, dict):
        return bool(value.get("enabled"))
    return bool(value)


def bucket_summary(config: Config) -> dict[str, Any]:
    """Collect the bucket facts worth printing or asserting on."""
    metadata = describe_bucket(config)
    if metadata is None:
        return {"exists": False, "name": config.bucket_name}
    return {
        "exists": True,
        "name": config.bucket_name,
        "location": (metadata.get("location") or "").lower(),
        "storage_class": metadata.get("default_storage_class") or metadata.get("storageClass"),
        "uniform_bucket_level_access": _flag_enabled(metadata.get("uniform_bucket_level_access")),
        "public_access_prevention": metadata.get("public_access_prevention"),
        "lifecycle_delete_ages": _current_lifecycle_ages(metadata),
    }


__all__ = [
    "ActionResult",
    "BucketOwnedElsewhereError",
    "apply_lifecycle",
    "bucket_exists",
    "bucket_summary",
    "create_bucket",
    "delete_object",
    "describe_bucket",
    "describe_object",
    "download_bytes",
    "ensure_bucket",
    "list_objects",
    "upload_bytes",
]
