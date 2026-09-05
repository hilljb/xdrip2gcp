#!/usr/bin/env python
"""Deploy the BigQuery-backed Nightscout function.

Shares the Stage 3 function's Secret Manager credential, so the password
already in xDrip works here too and switching the phone between the two
endpoints is only a URL change.

Deployment is skipped when nothing that affects the function has changed,
which includes the shared `nightscout_core` module even though it lives in the
other function's directory: it is copied in at deploy time, and its contents
are part of the hash.

Run from the repo root, after src/setup_bigquery.py:

    python src/deploy_bq_function.py
    python src/deploy_bq_function.py --show-url
    python src/deploy_bq_function.py --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xdrip2gcp import bigquery, cloudfunction, gcloud, secretmanager  # noqa: E402
from xdrip2gcp.config import ConfigError, ensure_password, load_config  # noqa: E402
from xdrip2gcp.function_source import shared_module_paths  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    parser.add_argument("--force", action="store_true", help="redeploy even if nothing changed")
    parser.add_argument(
        "--show-url", action="store_true", help="print the endpoint and the xDrip base URL"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    reason = gcloud.preflight(config)
    if reason is not None:
        print(f"cannot reach GCP: {reason}", file=sys.stderr)
        return 3

    function = config.function_bq
    shared = shared_module_paths()
    environment = cloudfunction.bq_function_env(config)

    print(f"function  {function.name} in {config.function_bq_region}")
    print(f"identity  {config.bq_runtime_service_account}")
    print(f"dataset   {config.entries_table_id}")
    print(f"shared    {', '.join(path.name for path in shared)}")

    if not bigquery.dataset_exists(config):
        print(
            f"\n{config.dataset_id} does not exist; run src/setup_bigquery.py first",
            file=sys.stderr,
        )
        return 4

    if args.dry_run:
        print("\ndry run: nothing was deployed")
        return 0

    config, generated = ensure_password(config)
    if generated:
        print("\ngenerated a Nightscout password and recorded it in resources/config.local.toml")

    # The same secret as the Stage 3 function, but this function runs as a
    # different identity, so it needs its own binding on it.
    print("\ncredential")
    for result in secretmanager.ensure_credential(config, config.bq_runtime_service_account):
        print(f"  [{result.marker}] {result}")

    print("\ndeploy")
    result = cloudfunction.deploy(
        config,
        force=args.force,
        function=function,
        env=environment,
        service_account=config.bq_runtime_service_account,
        shared_modules=shared,
    )
    print(f"  [{result.marker}] {result}")

    url = cloudfunction.function_url(config, function=function)
    if url:
        print(f"\nendpoint  {url}/api/v1/")
    if args.show_url and url:
        host = url.split("://", 1)[-1]
        print("\nxDrip base URL (Settings -> Cloud Upload -> Nightscout Sync):")
        print(f"  https://{config.auth.password}@{host}/api/v1/")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
