"""Idempotent Firestore provisioning, and a dependency-free document read.

The current reading lives in one Firestore document rather than in BigQuery.
The reasoning is in the plan, but briefly: BigQuery bills a 10 MiB minimum per
table referenced per query, so "what is the value right now" is the one
question it answers expensively, and a document that is overwritten every five
minutes and read often is exactly what Firestore is for.

Two things here mirror the BigQuery module. The database's location and mode
are fixed at creation, so `ensure_database` reports them rather than assuming
them. And the function's identity gets a custom role that can write this
document but cannot delete a database or export data.

Reads use the REST API with a token from `gcloud auth print-access-token`
rather than a client library: the local side of this repo deliberately has no
Python dependencies beyond the standard library, and one GET does not justify
breaking that.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import provision
from .actions import ActionResult
from .config import Config
from .gcloud import GcloudError, run

# Native mode is what supports realtime listeners, so a reader can be pushed
# each new value instead of polling for it.
DATABASE_TYPE = "firestore-native"

FIRESTORE_API = "https://firestore.googleapis.com/v1"
READ_TIMEOUT_SECONDS = 30

# Enough to create and overwrite one document, and nothing else. Notably absent:
# entities.delete, databases.delete, and the import/export permissions that
# could move data out of the project.
FIRESTORE_ROLE_PERMISSIONS = (
    "datastore.databases.get",
    "datastore.entities.create",
    "datastore.entities.get",
    "datastore.entities.update",
)

FIRESTORE_ROLE_TITLE = "xdrip2gcp current-value writer"
FIRESTORE_ROLE_DESCRIPTION = (
    "Publish the current CGM reading to one Firestore document. Cannot delete entities or databases."
)


def database_metadata(config: Config) -> dict[str, Any] | None:
    """The database's metadata, or None if the project has none."""
    result = run(
        config,
        ["firestore", "databases", "describe", f"--database={config.firestore.database}", "--format=json"],
        check=False,
    )
    if result.ok:
        return json.loads(result.stdout)
    text = f"{result.stdout}\n{result.stderr}".lower()
    if "not_found" in text or "not found" in text or "404" in text or "has not been used" in text:
        return None
    raise GcloudError(["firestore", "databases", "describe"], result.returncode, result.stdout, result.stderr)


def ensure_database(config: Config) -> ActionResult:
    """Create the Firestore database if the project has none.

    Mode and location cannot be changed afterwards, and a project gets one
    database in the free tier, so an existing database is reported as-is rather
    than being reconciled towards the configuration.
    """
    metadata = database_metadata(config)
    if metadata is not None:
        kind = (metadata.get("type") or "").lower()
        where = metadata.get("locationId") or "an unknown location"
        if kind and kind != "firestore_native":
            return ActionResult(
                False,
                f"database {config.firestore.database} exists in {where} but is {kind}, "
                "not Native mode; realtime listeners need Native mode and the mode cannot be changed",
            )
        return ActionResult(False, f"database {config.firestore.database} already exists in {where}")

    location = config.firestore_location
    run(
        config,
        [
            "firestore",
            "databases",
            "create",
            f"--database={config.firestore.database}",
            f"--location={location}",
            f"--type={DATABASE_TYPE}",
        ],
        timeout=300,
    )
    return ActionResult(True, f"created Native-mode database {config.firestore.database} in {location}")


def role_definition(config: Config) -> dict[str, Any] | None:
    role_id = config.service_accounts.firestore_role_id
    result = run(
        config,
        ["iam", "roles", "describe", role_id, f"--project={config.project_id}", "--format=json"],
        check=False,
    )
    if result.ok:
        return json.loads(result.stdout)
    if "NOT_FOUND" in result.stderr or "404" in result.stderr or "not found" in result.stderr.lower():
        return None
    raise GcloudError(["iam", "roles", "describe"], result.returncode, result.stdout, result.stderr)


def ensure_role(config: Config) -> ActionResult:
    """Create or update the custom write-but-never-delete role."""
    role_id = config.service_accounts.firestore_role_id
    wanted = sorted(FIRESTORE_ROLE_PERMISSIONS)
    current = role_definition(config)

    if current is not None:
        if sorted(current.get("includedPermissions") or []) == wanted:
            return ActionResult(False, f"custom role {role_id} already grants exactly what is needed")
        run(
            config,
            [
                "iam",
                "roles",
                "update",
                role_id,
                f"--project={config.project_id}",
                f"--permissions={','.join(wanted)}",
            ],
        )
        return ActionResult(True, f"updated custom role {role_id}")

    run(
        config,
        [
            "iam",
            "roles",
            "create",
            role_id,
            f"--project={config.project_id}",
            f"--title={FIRESTORE_ROLE_TITLE}",
            f"--description={FIRESTORE_ROLE_DESCRIPTION}",
            f"--permissions={','.join(wanted)}",
            "--stage=GA",
        ],
    )
    return ActionResult(True, f"created custom role {role_id} with {len(wanted)} permissions")


def ensure_access(config: Config) -> list[ActionResult]:
    """Give the function's existing identity permission to publish the value."""
    member = f"serviceAccount:{config.bq_runtime_service_account}"
    return [
        ensure_role(config),
        provision.ensure_project_role(config, member, config.firestore_role_name),
    ]


def ensure_all(config: Config) -> list[ActionResult]:
    return [ensure_database(config), *ensure_access(config)]


# --------------------------------------------------------------------------
# Reading the document
# --------------------------------------------------------------------------


def _access_token(config: Config) -> str:
    return run(config, ["auth", "print-access-token"]).stdout.strip()


def _decode(value: Any) -> Any:
    """Turn one Firestore REST value into an ordinary Python value.

    The REST API wraps every field in its type, so `{"integerValue": "123"}`
    rather than `123`, and integers arrive as strings because JSON cannot carry
    a 64-bit integer safely.
    """
    if not isinstance(value, dict):
        return value
    for key, convert in (
        ("stringValue", str),
        ("integerValue", int),
        ("doubleValue", float),
        ("booleanValue", bool),
        ("timestampValue", str),
    ):
        if key in value:
            return convert(value[key])
    if "nullValue" in value:
        return None
    if "mapValue" in value:
        return {k: _decode(v) for k, v in (value["mapValue"].get("fields") or {}).items()}
    if "arrayValue" in value:
        return [_decode(v) for v in (value["arrayValue"].get("values") or [])]
    return value


def get_document(config: Config) -> dict[str, Any] | None:
    """The published current reading, or None if nothing has been published."""
    url = f"{FIRESTORE_API}/{config.firestore_document_path}"
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {_access_token(config)}")

    try:
        with urllib.request.urlopen(request, timeout=READ_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        with error:
            if error.code == 404:
                return None
            detail = error.read().decode("utf-8", errors="replace")
        raise GcloudError(["firestore", "get", config.firestore.path], error.code, "", detail, program="")

    fields = payload.get("fields") or {}
    document = {name: _decode(value) for name, value in fields.items()}
    document["_updated"] = payload.get("updateTime")
    return document


def document_age_seconds(document: dict[str, Any]) -> float | None:
    """How long ago the published reading was taken."""
    epoch_ms = document.get("reading_epoch_ms")
    if epoch_ms is None:
        return None
    return datetime.now(timezone.utc).timestamp() - int(epoch_ms) / 1000


def summary(config: Config) -> dict[str, Any]:
    """The facts about the database worth printing."""
    metadata = database_metadata(config)
    if metadata is None:
        return {"exists": False, "database": config.firestore.database}
    return {
        "exists": True,
        "database": config.firestore.database,
        "type": metadata.get("type"),
        "location": metadata.get("locationId"),
        "document": config.firestore.path,
        "delete_protection": metadata.get("deleteProtectionState"),
    }
