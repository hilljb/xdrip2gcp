#!/usr/bin/env python
"""Write the generated test data into the configured bucket, idempotently.

Run from the repo root:

    python src/write_test_data.py
    python src/write_test_data.py --verify
    python src/write_test_data.py --clean
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xdrip2gcp import bucket, gcloud, testdata  # noqa: E402
from xdrip2gcp.config import ConfigError, load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--verify",
        action="store_true",
        help="read each object back and confirm it matches what was generated",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="delete the test objects instead of writing them",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(allow_suffix_generation=False)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    reason = gcloud.preflight(config)
    if reason is not None:
        print(f"cannot reach GCP: {reason}", file=sys.stderr)
        return 3

    if not bucket.bucket_exists(config):
        print(f"{config.bucket_uri} does not exist; run src/create_bucket.py first", file=sys.stderr)
        return 4

    payloads = testdata.all_payloads(config)

    try:
        if args.clean:
            for payload in payloads:
                result = bucket.delete_object(config, testdata.object_path(config, payload))
                print(f"[{'changed' if result.changed else 'no-op '}] {result.detail}")
            return 0

        for payload in payloads:
            path = testdata.object_path(config, payload)
            result = bucket.upload_bytes(config, path, payload.content)
            print(f"[{'changed' if result.changed else 'no-op '}] {result.detail}")

            if args.verify:
                fetched = bucket.download_bytes(config, path)
                if fetched != payload.content:
                    print(f"verification failed for {path}: content differs", file=sys.stderr)
                    return 6
                print(f"[verified] {path} round-tripped {len(fetched)} bytes intact")

        print("\nobjects under gs://{}/{}:".format(config.bucket_name, config.test_prefix))
        for path in bucket.list_objects(config, config.test_prefix):
            print(f"  {path}")
    except gcloud.GcloudError as error:
        print(f"gcloud failed: {error}", file=sys.stderr)
        return 5

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
