#!/usr/bin/env python
"""Show recent HTTP requests to the deployed function.

The quickest way to tell whether a phone is reaching the endpoint, and whether
its credential is accepted: every request appears with its method, status and
path. `gcloud functions logs read` is less useful here, because for
second-generation functions it returns request entries with an empty message.

Run from the repo root:

    python src/show_requests.py
    python src/show_requests.py --limit 50 --minutes 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xdrip2gcp import gcloud  # noqa: E402
from xdrip2gcp.config import ConfigError, load_config  # noqa: E402

STATUS_MEANINGS = {
    200: "accepted and written",
    400: "payload was not usable JSON",
    401: "credential missing or wrong",
    404: "endpoint not implemented",
    405: "wrong method for that endpoint",
    413: "payload too large",
    500: "server error; check the function logs",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=20, help="how many requests to show")
    parser.add_argument("--minutes", type=int, default=0, help="only requests from the last N minutes")
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

    # A second-generation function is a Cloud Run service underneath, so its
    # request logs live under cloud_run_revision.
    filters = [
        'resource.type="cloud_run_revision"',
        f'resource.labels.service_name="{config.function.name}"',
        'httpRequest.requestMethod!=""',
    ]
    if args.minutes:
        filters.append(f'timestamp>="-{args.minutes}m"')

    try:
        result = gcloud.run(
            config,
            [
                "logging",
                "read",
                " AND ".join(filters),
                f"--limit={args.limit}",
                "--format=table[no-heading](timestamp.date('%Y-%m-%d %H:%M:%S'),"
                "httpRequest.status,httpRequest.requestMethod,httpRequest.requestUrl)",
            ],
        )
    except gcloud.GcloudError as error:
        print(f"gcloud failed: {error}", file=sys.stderr)
        return 5

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        window = f" in the last {args.minutes} minutes" if args.minutes else ""
        print(f"no requests to {config.function.name}{window}")
        return 0

    seen_statuses = set()
    print(f"{'TIME (UTC)':19}  {'STATUS':6}  {'METHOD':6}  PATH")
    for line in reversed(lines):
        fields = line.split()
        if len(fields) < 4:
            continue
        timestamp = " ".join(fields[:2])
        status, method, url = fields[2], fields[3], fields[-1]
        path = url.split("://", 1)[-1].partition("/")[2]
        seen_statuses.add(status)
        print(f"{timestamp:19}  {status:6}  {method:6}  /{path}")

    explained = [
        (code, meaning) for code, meaning in STATUS_MEANINGS.items() if str(code) in seen_statuses
    ]
    if explained:
        print()
        for code, meaning in explained:
            print(f"  {code}: {meaning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
