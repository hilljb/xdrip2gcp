#!/usr/bin/env python
"""Prepare the Firestore side of Stage 5: the database that holds the current value.

The current reading is one document, overwritten every five minutes. It is here
rather than in BigQuery because BigQuery bills a 10 MiB minimum per table per
query, which makes "what is the value right now" the one question a warehouse
answers expensively.

Safe to run repeatedly. A second run reports no-ops.

Two things cannot be undone, so this reports what it finds instead of trying to
reconcile it: a project gets one free Firestore database, and that database's
mode and location are fixed when it is created. Native mode is what supports
the realtime listeners that let a reader be pushed each new value.

Run from the repo root:

    python src/setup_firestore.py
    python src/setup_firestore.py --dry-run
    python src/setup_firestore.py --show
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xdrip2gcp import firestore, gcloud, provision  # noqa: E402
from xdrip2gcp.config import ConfigError, load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be created, change nothing"
    )
    parser.add_argument(
        "--show", action="store_true", help="print the published document and exit"
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

    if args.show:
        document = firestore.get_document(config)
        if document is None:
            print(f"nothing published at {config.firestore.path} yet")
            return 1
        print(json.dumps(document, indent=2, sort_keys=True))
        age = firestore.document_age_seconds(document)
        if age is not None:
            print(f"\nthe reading is {age / 60:.1f} minutes old")
        return 0

    print(f"project   {config.project_id}")
    print(f"database  {config.firestore.database} in {config.firestore_location} ({firestore.DATABASE_TYPE})")
    print(f"document  {config.firestore.path}")
    print(f"identity  {config.bq_runtime_service_account}")
    print(f"role      {config.firestore_role_name}")

    if args.dry_run:
        print("\ndry run: nothing was created")
        return 0

    print("\nAPIs")
    for result in provision.enable_services(config):
        print(f"  [{result.marker}] {result}")

    print("\ndatabase")
    for result in firestore.ensure_all(config):
        print(f"  [{result.marker}] {result}")

    facts = firestore.summary(config)
    if facts.get("exists"):
        print("\nverified")
        print(f"  mode       {facts['type']}")
        print(f"  location   {facts['location']}")
        print(f"  document   {facts['document']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
