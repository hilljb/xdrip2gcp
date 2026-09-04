#!/usr/bin/env python
"""Deploy the Nightscout Cloud Function, idempotently.

Generates the Nightscout password on first run, stores its scrypt digest in
Secret Manager, and deploys the function. Re-running is a no-op unless the
source, the deploy settings or the credential changed.

Run from the repo root:

    python src/deploy_function.py
    python src/deploy_function.py --dry-run
    python src/deploy_function.py --force
    python src/deploy_function.py --show-url
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xdrip2gcp import bucket, cloudfunction, gcloud, secretmanager  # noqa: E402
from xdrip2gcp.config import ConfigError, ensure_password, load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report current state without changing anything")
    parser.add_argument("--force", action="store_true", help="redeploy even when nothing has changed")
    parser.add_argument(
        "--show-url",
        action="store_true",
        help="print the endpoint and the xDrip base URL, then exit",
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

    try:
        if args.show_url:
            url = cloudfunction.function_url(config)
            if url is None:
                print(f"{config.function.name} is not deployed", file=sys.stderr)
                return 4
            print(url)
            if config.auth.password:
                host = url.split("://", 1)[-1]
                # The format xDrip's Nightscout uploader expects.
                print(f"xDrip base URL: https://{config.auth.password}@{host}/api/v1/")
            return 0

        if args.dry_run:
            print(json.dumps(cloudfunction.summary(config), indent=2, sort_keys=True))
            return 0

        if not bucket.bucket_exists(config):
            print(f"{config.bucket_uri} does not exist; run src/create_bucket.py first", file=sys.stderr)
            return 4

        config, generated = ensure_password(config)
        if generated:
            print("[changed] generated a Nightscout password into resources/config.local.toml")
        else:
            print("[no-op  ] using the Nightscout password from resources/config.local.toml")

        for result in secretmanager.ensure_credential(config):
            print(f"[{result.marker}] {result.detail}")

        result = cloudfunction.deploy(config, force=args.force)
        print(f"[{result.marker}] {result.detail}")

        print(json.dumps(cloudfunction.summary(config), indent=2, sort_keys=True))
    except gcloud.GcloudError as error:
        print(f"gcloud failed: {error}", file=sys.stderr)
        return 5

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
