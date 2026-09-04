#!/usr/bin/env python
"""Prepare the GCP project for the Cloud Function, idempotently.

Enables the required APIs and creates the two dedicated service accounts with
their least-privilege roles. Safe to re-run; a second run reports no-ops.

Run from the repo root:

    python src/setup_gcp.py
    python src/setup_gcp.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xdrip2gcp import bucket, gcloud, provision  # noqa: E402
from xdrip2gcp.config import ConfigError, load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what is missing without changing anything",
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

    print(f"project: {config.project_id}")
    print(f"bucket:  {config.bucket_uri}")
    print(f"runtime identity: {config.runtime_service_account}")
    print(f"build identity:   {config.build_service_account}\n")

    try:
        if args.dry_run:
            enabled = provision.enabled_services(config)
            for service in config.services:
                state = "enabled" if service in enabled else "MISSING"
                print(f"  {state:>8}  {service}")
            for account_id in (config.service_accounts.runtime_id, config.service_accounts.build_id):
                email = config.service_account_email(account_id)
                state = "exists" if provision.service_account_exists(config, email) else "MISSING"
                print(f"  {state:>8}  {email}")
            return 0

        # The runtime identity is granted access on the bucket, so the bucket
        # has to exist before the binding can be applied.
        if not bucket.bucket_exists(config):
            print(
                f"{config.bucket_uri} does not exist; run src/create_bucket.py first",
                file=sys.stderr,
            )
            return 4

        for result in provision.enable_services(config):
            print(f"[{result.marker}] {result.detail}")
        for result in provision.ensure_service_accounts(config):
            print(f"[{result.marker}] {result.detail}")
    except gcloud.GcloudError as error:
        print(f"gcloud failed: {error}", file=sys.stderr)
        return 5

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
