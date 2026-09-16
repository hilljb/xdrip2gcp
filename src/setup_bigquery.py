#!/usr/bin/env python
"""Prepare the BigQuery side of Stage 5: dataset, table, view and identity.

Safe to run repeatedly. A second run reports no-ops, and a column added to
`bq_core.SCHEMA` is applied to the existing table rather than needing a
migration.

Nothing here sets an expiration of any kind, and the identity this grants can
only append: the custom role carries no delete, no query and no read.

The current reading is not here; it is one Firestore document, provisioned by
`setup_firestore.py`.

Run from the repo root:

    python src/setup_bigquery.py
    python src/setup_bigquery.py --dry-run
    python src/setup_bigquery.py --drop-table entries_latest
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xdrip2gcp import bigquery, gcloud, provision  # noqa: E402
from xdrip2gcp.config import ConfigError, load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be created, change nothing"
    )
    parser.add_argument(
        "--drop-table",
        metavar="NAME",
        help="delete a table in the dataset by name, for retiring one the design has moved past",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(allow_generation=False)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    reason = gcloud.preflight(config)
    if reason is not None:
        print(f"cannot reach GCP: {reason}", file=sys.stderr)
        return 3
    if not gcloud.bq_available():
        print("the bq CLI is not on PATH; it ships with the Cloud SDK", file=sys.stderr)
        return 3

    print(f"project   {config.project_id}")
    print(f"dataset   {config.dataset_id} in {config.bigquery_location}")
    print(f"table     {config.bigquery.entries_table}")
    print(f"view      {config.bigquery.current_view}")
    print(f"identity  {config.bq_runtime_service_account}")
    print(f"role      {config.bq_role_name}")

    if args.dry_run:
        print("\ndry run: nothing was created")
        return 0

    print("\nAPIs")
    for result in provision.enable_services(config):
        print(f"  [{result.marker}] {result}")

    print("\nidentity")
    for result in bigquery.ensure_identity(config):
        print(f"  [{result.marker}] {result}")

    print("\ndataset")
    for result in bigquery.ensure_dataset_objects(config):
        print(f"  [{result.marker}] {result}")

    if args.drop_table:
        print("\nretire")
        result = bigquery.drop_table(config, args.drop_table)
        print(f"  [{result.marker}] {result}")

    facts = bigquery.summary(config)
    if facts.get("exists"):
        print("\nverified")
        print(f"  partitioned by {facts['partition_field']} ({facts['partition_type']})")
        print(f"  clustered by   {', '.join(facts['clustered_by']) or 'nothing'}")
        print(f"  columns        {len(facts['columns'])}")
        print(f"  rows           {facts['rows']}")
        expiry = facts["partition_expiration_ms"] or facts["expiration_time"]
        print(f"  expiration     {expiry or 'none, as intended'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
