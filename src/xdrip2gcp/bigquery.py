"""Idempotent BigQuery provisioning: dataset, tables, view and IAM.

Everything is expressed as DDL that converges rather than as create calls that
fail the second time: `CREATE SCHEMA IF NOT EXISTS`, `CREATE TABLE IF NOT
EXISTS`, `ALTER TABLE ADD COLUMN IF NOT EXISTS`. A second run reports no-ops,
and adding a column to `bq_core.SCHEMA` later is picked up by the next run
instead of needing a migration.

Two decisions here are about permanence rather than convenience:

* Nothing sets a default table expiration or a partition expiration, and this
  module never issues a drop. Stage 5 data is meant to outlive every other
  part of the project.
* The function's identity gets a custom role rather than
  `roles/bigquery.dataEditor`. That role would let the endpoint delete the
  tables it writes to, which is exactly the authority it should not have.
"""

from __future__ import annotations

import json
from typing import Any

from . import provision
from .actions import ActionResult
from .config import Config
from .function_source import bq_core_module
from .gcloud import GcloudError, bq_query, run, run_bq

# What the runtime identity needs and nothing else: read the table's schema and
# append rows to it. The function is append-only now that the current reading is
# published to Firestore instead of maintained in a table here, so it needs no
# query jobs and no read access at all. Notably absent: tables.delete,
# tables.create, tables.getData, jobs.create, datasets.delete.
BQ_ROLE_PERMISSIONS = (
    "bigquery.tables.get",
    "bigquery.tables.updateData",
)

BQ_ROLE_TITLE = "xdrip2gcp BigQuery writer"
BQ_ROLE_DESCRIPTION = (
    "Append readings to the entries table. Cannot read, query, or delete anything."
)


def _core():
    return bq_core_module()


# SQL takes `project.dataset.table`; the CLI's own arguments want
# `project:dataset.table`.
def _cli_dataset(config: Config) -> str:
    return f"{config.project_id}:{config.bigquery.dataset}"


def _cli_table(config: Config, table: str) -> str:
    return f"{_cli_dataset(config)}.{table}"


# The bq CLI reports errors on stdout rather than stderr, so both streams have
# to be considered before deciding whether a failure means "absent".
def _reports_missing(result) -> bool:
    text = f"{result.stdout}\n{result.stderr}".lower()
    return "not found" in text or "404" in text


def dataset_exists(config: Config) -> bool:
    result = run_bq(config, ["show", "--format=none", _cli_dataset(config)], check=False)
    if result.ok:
        return True
    if _reports_missing(result):
        return False
    raise GcloudError(
        ["show", _cli_dataset(config)], result.returncode, result.stdout, result.stderr, program="bq"
    )


def ensure_dataset(config: Config) -> ActionResult:
    """Create the dataset in the configured location if it is not there.

    A dataset's location is fixed at creation, so this is the one setting that
    cannot be corrected by a later run; it is reported rather than assumed.
    """
    if dataset_exists(config):
        return ActionResult(False, f"dataset {config.dataset_id} already exists")

    location = config.bigquery_location
    bq_query(
        config,
        f"CREATE SCHEMA IF NOT EXISTS `{config.dataset_id}` "
        f"OPTIONS (location = '{location}', "
        "description = 'xDrip CGM data. No default table expiration: this data is permanent.')",
        location=location,
    )
    return ActionResult(True, f"created dataset {config.dataset_id} in {location}")


def table_metadata(config: Config, table: str) -> dict[str, Any] | None:
    result = run_bq(config, ["show", "--format=prettyjson", _cli_table(config, table)], check=False)
    if result.ok:
        return json.loads(result.stdout)
    if _reports_missing(result):
        return None
    raise GcloudError(
        ["show", _cli_table(config, table)], result.returncode, result.stdout, result.stderr, program="bq"
    )


def ensure_entries_table(config: Config) -> ActionResult:
    """Create the raw, day-partitioned readings table."""
    core = _core()
    table = config.bigquery.entries_table
    existed = table_metadata(config, table) is not None

    bq_query(
        config,
        core.create_entries_ddl(config.quoted_table(table)),
        location=config.bigquery_location,
    )
    if existed:
        return ActionResult(False, f"table {config.table_id(table)} already exists")
    return ActionResult(
        True,
        f"created {config.table_id(table)}, partitioned by {core.PARTITION_COLUMN}, "
        f"clustered by {core.CLUSTER_COLUMN}",
    )


def existing_columns(config: Config, table: str) -> list[str]:
    metadata = table_metadata(config, table)
    if metadata is None:
        return []
    fields = (metadata.get("schema") or {}).get("fields") or []
    return [str(field.get("name")) for field in fields]


def ensure_columns(config: Config, table: str) -> ActionResult:
    """Add any column the schema has gained since the table was created.

    Columns are only ever added, never dropped or retyped, so a schema change
    is applied without touching the data already stored.
    """
    core = _core()
    present = set(existing_columns(config, table))
    missing = [column for column in core.SCHEMA if column.name not in present]
    if not missing:
        return ActionResult(False, f"{config.table_id(table)} schema is up to date")

    for column in missing:
        # An added column cannot be REQUIRED, whatever the schema says: the
        # rows already stored would violate it.
        bq_query(
            config,
            f"ALTER TABLE {config.quoted_table(table)} "
            f"ADD COLUMN IF NOT EXISTS {column.name} {column.type}",
            location=config.bigquery_location,
        )
    names = ", ".join(column.name for column in missing)
    return ActionResult(True, f"added {len(missing)} column(s) to {config.table_id(table)}: {names}")


def view_definition(config: Config, view: str) -> str | None:
    metadata = table_metadata(config, view)
    if metadata is None:
        return None
    return ((metadata.get("view") or {}).get("query")) or None


def ensure_current_view(config: Config) -> ActionResult:
    """Create or update the view that collapses duplicate readings.

    `CREATE OR REPLACE VIEW` would succeed unconditionally, so the current
    definition is compared first; that keeps a re-run honest about having
    changed nothing.
    """
    core = _core()
    view = config.bigquery.current_view
    sql = core.create_current_view_sql(
        config.quoted_table(view), config.quoted_table(config.bigquery.entries_table)
    )

    body = core.current_view_body(config.quoted_table(config.bigquery.entries_table))
    current = view_definition(config, view)
    if current is not None and current.strip() == body.strip():
        return ActionResult(False, f"view {config.table_id(view)} is already current")

    bq_query(config, sql, location=config.bigquery_location)
    verb = "replaced" if current is not None else "created"
    return ActionResult(True, f"{verb} view {config.table_id(view)}")


def role_definition(config: Config) -> dict[str, Any] | None:
    result = run(
        config,
        ["iam", "roles", "describe", config.service_accounts.bq_role_id, f"--project={config.project_id}", "--format=json"],
        check=False,
    )
    if result.ok:
        return json.loads(result.stdout)
    if "NOT_FOUND" in result.stderr or "404" in result.stderr or "not found" in result.stderr.lower():
        return None
    raise GcloudError(["iam", "roles", "describe"], result.returncode, result.stdout, result.stderr)


def ensure_bq_role(config: Config) -> ActionResult:
    """Create or update the custom write-but-never-delete role."""
    role_id = config.service_accounts.bq_role_id
    wanted = sorted(BQ_ROLE_PERMISSIONS)
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
            f"--title={BQ_ROLE_TITLE}",
            f"--description={BQ_ROLE_DESCRIPTION}",
            f"--permissions={','.join(wanted)}",
            "--stage=GA",
        ],
    )
    return ActionResult(True, f"created custom role {role_id} with {len(wanted)} permissions")


def ensure_identity(config: Config) -> list[ActionResult]:
    """Create the BigQuery function's identity and grant it the custom role.

    The binding is project-level rather than on the dataset, which is looser
    than it needs to be, but the role carries only "read this table's schema"
    and "append to it" and the project holds exactly one dataset.
    """
    accounts = config.service_accounts
    results = [
        provision.ensure_service_account(
            config, accounts.bq_runtime_id, "xdrip2gcp BigQuery function runtime"
        ),
        ensure_bq_role(config),
    ]
    member = f"serviceAccount:{config.bq_runtime_service_account}"
    results.append(provision.ensure_project_role(config, member, config.bq_role_name))
    return results


def ensure_dataset_objects(config: Config) -> list[ActionResult]:
    """Bring the dataset, its table and its view to the configured state."""
    results = [ensure_dataset(config)]
    results.append(ensure_entries_table(config))
    results.append(ensure_columns(config, config.bigquery.entries_table))
    results.append(ensure_current_view(config))
    return results


def drop_table(config: Config, table: str) -> ActionResult:
    """Delete a table, for retiring one the design has moved past.

    Deliberately not something the function's identity can do: this runs as the
    operator, from a script, against a table named explicitly.
    """
    if table_metadata(config, table) is None:
        return ActionResult(False, f"table {config.table_id(table)} is already gone")
    run_bq(config, ["rm", "--table", "--force", _cli_table(config, table)])
    return ActionResult(True, f"deleted table {config.table_id(table)}")


def query_rows(config: Config, sql: str) -> list[dict[str, Any]]:
    """Run a query and return its rows, or [] when it returns none."""
    result = bq_query(config, sql, location=config.bigquery_location)
    text = result.stdout.strip()
    if not text:
        return []
    return json.loads(text)


def summary(config: Config) -> dict[str, Any]:
    """The facts about the dataset worth printing."""
    bigquery = config.bigquery
    entries = table_metadata(config, bigquery.entries_table)
    if entries is None:
        return {"exists": False, "dataset": config.dataset_id}

    partitioning = entries.get("timePartitioning") or {}
    clustering = entries.get("clustering") or {}
    return {
        "exists": True,
        "dataset": config.dataset_id,
        "location": entries.get("location") or config.bigquery_location,
        "entries_table": config.entries_table_id,
        "rows": int(entries.get("numRows") or 0),
        "bytes": int(entries.get("numBytes") or 0),
        "partition_field": partitioning.get("field"),
        "partition_type": partitioning.get("type"),
        "partition_expiration_ms": partitioning.get("expirationMs"),
        "clustered_by": clustering.get("fields") or [],
        "columns": existing_columns(config, bigquery.entries_table),
        "view": config.current_view_id,
        "expiration_time": entries.get("expirationTime"),
    }
