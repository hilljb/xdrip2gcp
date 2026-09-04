#!/usr/bin/env python
"""Create the configured Cloud Storage test bucket, idempotently.

Run from the repo root:

    python src/create_bucket.py
    python src/create_bucket.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xdrip2gcp import bucket, gcloud  # noqa: E402
from xdrip2gcp.config import ConfigError, load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the resolved configuration and current bucket state without changing anything",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    print(f"project:  {config.project_id}")
    print(f"bucket:   {config.bucket_uri}")
    print(f"location: {config.location}")
    print(f"gcloud python: {config.cloudsdk_python}")

    reason = gcloud.preflight(config)
    if reason is not None:
        print(f"cannot reach GCP: {reason}", file=sys.stderr)
        return 3

    try:
        if args.dry_run:
            print(json.dumps(bucket.bucket_summary(config), indent=2, sort_keys=True))
            return 0

        for result in bucket.ensure_bucket(config):
            marker = "changed" if result.changed else "no-op "
            print(f"[{marker}] {result.detail}")

        print(json.dumps(bucket.bucket_summary(config), indent=2, sort_keys=True))
    except bucket.BucketOwnedElsewhereError as error:
        print(f"bucket name unavailable: {error}", file=sys.stderr)
        return 4
    except gcloud.GcloudError as error:
        print(f"gcloud failed: {error}", file=sys.stderr)
        return 5

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
