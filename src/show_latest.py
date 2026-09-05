#!/usr/bin/env python
"""Show the most recent reading stored in the bucket.

`show_requests.py` answers "is the phone reaching the endpoint"; this answers
"what actually landed". Handy after changing anything on the phone, and as a
quick check that a reading is as fresh as it should be.

Object names carry the reading's own epoch-millisecond timestamp, so the newest
data is found by listing names rather than by reading every object: only the
last few objects of the newest day are downloaded.

Run from the repo root:

    python src/show_latest.py
    python src/show_latest.py --count 10
    python src/show_latest.py --collection devicestatus --raw

Exit codes: 4 means the bucket holds no data for that collection yet.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xdrip2gcp import bucket, gcloud  # noqa: E402
from xdrip2gcp.config import Config, ConfigError, load_config  # noqa: E402
from xdrip2gcp.function_source import core_module  # noqa: E402

PARTITION = re.compile(r"/dt=(\d{4}-\d{2}-\d{2})/")
TIMESTAMP_PREFIX = re.compile(r"^(\d{13})-")

# A batch is named for its *earliest* reading, so a batch that sorts slightly
# earlier can still hold the newest reading. Reading a few extra objects costs
# one gcloud call each and removes that edge case in practice.
LOOKBACK = 3


def partition_day(path: str) -> str:
    """The `dt=` day a path belongs to, or "" if it has none."""
    match = PARTITION.search(path)
    return match.group(1) if match else ""


def sort_key(path: str) -> tuple[int, str]:
    """Order objects within a day by the timestamp their name carries.

    Names without a timestamp prefix (device status, which carries no time of
    its own) sort first, since nothing better is known about them.
    """
    name = path.rsplit("/", 1)[-1]
    match = TIMESTAMP_PREFIX.match(name)
    return (int(match.group(1)) if match else -1, name)


def partitions(paths: list[str]) -> list[list[str]]:
    """Group object paths by day, newest day first, each day newest last."""
    days: dict[str, list[str]] = {}
    for path in paths:
        days.setdefault(partition_day(path), []).append(path)
    return [sorted(days[day], key=sort_key) for day in sorted(days, reverse=True)]


def latest_documents(
    paths: list[str],
    fetch: Callable[[str], bytes],
    count: int,
    timestamp_of: Callable[[dict[str, Any]], int | None],
) -> list[tuple[dict[str, Any], str]]:
    """The `count` newest documents, each paired with the object holding it.

    Walks days newest first and, within a day, objects newest first, fetching
    only as many as it takes to fill `count`. A day whose names carry no
    timestamps has no meaningful order, so all of it is read.
    """
    collected: list[tuple[dict[str, Any], str]] = []

    for day in partitions(paths):
        ordered = list(reversed(day))
        unordered = all(sort_key(path)[0] < 0 for path in day)
        wanted = len(ordered) if unordered else count + LOOKBACK

        for path in ordered[:wanted]:
            for line in fetch(path).decode("utf-8").splitlines():
                line = line.strip()
                if line:
                    collected.append((json.loads(line), path))

        if len(collected) >= count:
            break

    collected.sort(key=lambda pair: timestamp_of(pair[0]) or 0, reverse=True)
    return collected[:count]


def human_age(seconds: float) -> str:
    if seconds < 0:
        return "in the future"
    if seconds < 90:
        return f"{int(seconds)} seconds old"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes old"
    if seconds < 172800:
        return f"{round(seconds / 3600)} hours old"
    return f"{round(seconds / 86400)} days old"


def format_delta(value: Any) -> str:
    try:
        return f"{float(value):+.1f}"
    except (TypeError, ValueError):
        return "-"


def local_time(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return "unknown"
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def print_entries(
    documents: list[tuple[dict[str, Any], str]],
    timestamp_of: Callable[[dict[str, Any]], int | None],
) -> None:
    print(f"{'TIME (LOCAL)':19}  {'MG/DL':5}  {'DELTA':6}  {'DIRECTION':10}  DEVICE")
    for document, _ in documents:
        print(
            f"{local_time(timestamp_of(document)):19}  "
            f"{str(document.get('sgv', '-')):5}  "
            f"{format_delta(document.get('delta')):6}  "
            f"{str(document.get('direction', '-')):10}  "
            f"{document.get('device', '-')}"
        )


def print_raw(documents: list[tuple[dict[str, Any], str]]) -> None:
    for document, path in documents:
        print(f"# {path}")
        print(json.dumps(document, sort_keys=True))


def report(
    config: Config,
    collection: str,
    count: int,
    raw: bool,
    fetch: Callable[[str], bytes],
    paths: list[str],
) -> int:
    core = core_module()
    documents = latest_documents(paths, fetch, count, core.document_timestamp_ms)
    if not documents:
        print(f"objects exist under {collection} but hold no documents", file=sys.stderr)
        return 4

    # Only entries have a fixed shape worth tabulating; treatments and device
    # status vary by what the phone chose to send.
    if raw or collection != "entries":
        print_raw(documents)
    else:
        print_entries(documents, core.document_timestamp_ms)

    newest, path = documents[0]
    timestamp = core.document_timestamp_ms(newest)
    print()
    if timestamp is None:
        print(f"newest document carries no timestamp ({path})")
    else:
        age = (datetime.now(timezone.utc).timestamp() * 1000 - timestamp) / 1000
        print(f"newest reading is {human_age(age)} ({path})")
    return 0


def main(argv: list[str] | None = None) -> int:
    core = core_module()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--collection",
        default="entries",
        choices=core.WRITABLE_COLLECTIONS,
        help="which collection to read (default: entries)",
    )
    parser.add_argument("--count", type=int, default=1, help="how many documents to show")
    parser.add_argument("--raw", action="store_true", help="print stored JSON instead of a table")
    args = parser.parse_args(argv)

    if args.count < 1:
        parser.error("--count must be at least 1")

    try:
        config = load_config(allow_generation=False)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    reason = gcloud.preflight(config)
    if reason is not None:
        print(f"cannot reach GCP: {reason}", file=sys.stderr)
        return 3

    prefix = f"{config.data_prefix}/collection={args.collection}/"
    try:
        paths = bucket.list_objects(config, prefix)
    except gcloud.GcloudError as error:
        print(f"gcloud failed: {error}", file=sys.stderr)
        return 5

    if not paths:
        print(f"no {args.collection} stored in {config.bucket_uri} yet", file=sys.stderr)
        return 4

    def fetch(path: str) -> bytes:
        return bucket.download_bytes(config, path)

    return report(config, args.collection, args.count, args.raw, fetch, paths)


if __name__ == "__main__":
    raise SystemExit(main())
