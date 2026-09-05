#!/usr/bin/env python
"""Print shell exports for the values the project's gcloud commands need.

Use it so that instance-specific names — the bucket's random suffix, the
function's generated hostname — never have to be written down anywhere:

    eval "$(python src/config_env.py)"
    gcloud storage ls --recursive "$XDRIP2GCP_BUCKET_URI/cgm-data/**"

The Nightscout password is deliberately *not* included. Exported variables end
up in the environment of every child process and in shell history, which is the
wrong place for a credential; use `deploy_function.py --show-url` when you need
it.

None of the exported names are ones `config.py` treats as overrides, so a stale
value in your shell cannot silently change which bucket or project the Python
code resolves.

Run from the repo root:

    eval "$(python src/config_env.py)"
    python src/config_env.py --no-remote     # skip the deployed function lookup
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xdrip2gcp import cloudfunction, gcloud  # noqa: E402
from xdrip2gcp.config import ConfigError, load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--no-remote",
        action="store_true",
        help="do not call gcloud to look up the deployed function's URL",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(allow_generation=False)
    except ConfigError as error:
        print(f"# configuration error: {error}", file=sys.stderr)
        return 2

    exports = {
        "CLOUDSDK_PYTHON": config.cloudsdk_python,
        "XDRIP2GCP_PROJECT": config.project_id,
        "XDRIP2GCP_BUCKET": config.bucket_name,
        "XDRIP2GCP_BUCKET_URI": config.bucket_uri,
        "XDRIP2GCP_FUNCTION": config.function.name,
        "XDRIP2GCP_REGION": config.function_region,
        # Stage 5. The dataset and table names are not instance-specific, but
        # having them here means a query can be pasted without looking them up.
        "XDRIP2GCP_BQ_FUNCTION": config.function_bq.name,
        "XDRIP2GCP_DATASET": config.dataset_id,
        "XDRIP2GCP_ENTRIES": config.entries_table_id,
        "XDRIP2GCP_CURRENT": config.current_view_id,
        "XDRIP2GCP_LATEST": config.latest_table_id,
    }

    urls: dict[str, str | None] = {"XDRIP2GCP_URL": None, "XDRIP2GCP_BQ_URL": None}
    if not args.no_remote and gcloud.preflight(config) is None:
        urls["XDRIP2GCP_URL"] = cloudfunction.function_url(config)
        urls["XDRIP2GCP_BQ_URL"] = cloudfunction.function_url(config, function=config.function_bq)
    exports.update({name: url for name, url in urls.items() if url})

    print("# eval \"$(python src/config_env.py)\"")
    for name, value in exports.items():
        print(f"export {name}={shlex.quote(value)}")

    if not args.no_remote:
        for name, url in urls.items():
            if url is None:
                print(f"# {name} omitted: that function is not deployed or gcloud is unavailable")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
