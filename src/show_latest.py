#!/usr/bin/env python
"""Show the most recent reading that was stored.

`show_requests.py` answers "is the phone reaching the endpoint"; this answers
"what actually landed". Handy after changing anything on the phone, and as a
quick check that a reading is as fresh as it should be.

Both destinations are readable, since the phone can be pointed at either
endpoint:

    --source bigquery  (default)  the Stage 5 dataset
    --source bucket               the Stage 3 bucket

Each is read the cheap way. In the bucket, object names carry the reading's own
epoch-millisecond timestamp, so the newest data is found by listing names
rather than by reading every object. In BigQuery, a request for no more
readings than the latest-readings table holds is answered from that table,
which is two rows, rather than from the history; the footer says which table
answered.

Run from the repo root:

    python src/show_latest.py
    python src/show_latest.py --count 10
    python src/show_latest.py --source bucket --collection devicestatus --raw

Exit codes: 4 means nothing is stored there yet.
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

from xdrip2gcp import bigquery, bucket, gcloud  # noqa: E402
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


# How far back the first BigQuery query looks, then how far it widens to. The
# point of the first window is to touch one or two partitions in the ordinary
# case; the wider ones are for coming back to a project that has been idle.
WINDOWS_DAYS = (2, 30, None)


def bigquery_rows(config: Config, count: int) -> tuple[list[dict[str, Any]], str]:
    """The newest readings from BigQuery, and the name of the table read.

    Both timestamps are already stored, so this converts nothing: the local
    wall clock and the zone it was in are columns, which is the whole point of
    storing them.
    """
    columns = (
        "FORMAT_DATETIME('%Y-%m-%d %H:%M:%S', reading_time_local) AS local_text, "
        "local_zone, sgv, delta, direction, device, "
        "UNIX_MILLIS(reading_time_utc) AS utc_ms, "
        "TO_JSON_STRING(raw) AS raw_text"
    )

    # The latest-readings table exists precisely to answer this question
    # without touching the history, so use it when it can.
    if count <= config.bigquery.latest_rows:
        table = config.bigquery.latest_table
        rows = bigquery.query_rows(
            config,
            f"SELECT {columns} FROM {config.quoted_table(table)} "
            f"ORDER BY reading_time_utc DESC LIMIT {count}",
        )
        if rows:
            return rows, config.table_id(table)

    view = config.quoted_table(config.bigquery.current_view)
    for days in WINDOWS_DAYS:
        window = (
            f"WHERE reading_date_utc >= DATE_SUB(CURRENT_DATE('UTC'), INTERVAL {days} DAY) "
            if days is not None
            else ""
        )
        rows = bigquery.query_rows(
            config,
            f"SELECT {columns} FROM {view} {window}ORDER BY reading_time_utc DESC LIMIT {count}",
        )
        if rows:
            return rows, config.current_view_id
    return [], config.current_view_id


def print_bigquery_rows(rows: list[dict[str, Any]]) -> None:
    print(f"{'TIME (LOCAL)':19}  {'ZONE':4}  {'MG/DL':5}  {'DELTA':6}  {'DIRECTION':10}  DEVICE")
    for row in rows:
        print(
            f"{row.get('local_text') or 'unknown':19}  "
            f"{row.get('local_zone') or '-':4}  "
            f"{str(row.get('sgv') or '-'):5}  "
            f"{format_delta(row.get('delta')):6}  "
            f"{str(row.get('direction') or '-'):10}  "
            f"{row.get('device') or '-'}"
        )


def report_bigquery(config: Config, count: int, raw: bool) -> int:
    rows, source = bigquery_rows(config, count)
    if not rows:
        print(f"no readings stored in {config.entries_table_id} yet", file=sys.stderr)
        return 4

    if raw:
        for row in rows:
            print(row.get("raw_text") or "{}")
    else:
        print_bigquery_rows(rows)

    newest = rows[0].get("utc_ms")
    print()
    if newest is None:
        print(f"newest reading has no timestamp (from {source})")
    else:
        age = (datetime.now(timezone.utc).timestamp() * 1000 - int(newest)) / 1000
        print(f"newest reading is {human_age(age)} (from {source})")
    return 0


def main(argv: list[str] | None = None) -> int:
    core = core_module()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        default="bigquery",
        choices=("bigquery", "bucket"),
        help="where to read from (default: bigquery)",
    )
    parser.add_argument(
        "--collection",
        default="entries",
        choices=core.WRITABLE_COLLECTIONS,
        help="which collection to read; bucket only, BigQuery stores entries alone",
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

    if args.source == "bigquery":
        if args.collection != "entries":
            parser.error("BigQuery stores only entries; use --source bucket for other collections")
        if not gcloud.bq_available():
            print("the bq CLI is not on PATH; it ships with the Cloud SDK", file=sys.stderr)
            return 3
        try:
            return report_bigquery(config, args.count, args.raw)
        except gcloud.GcloudError as error:
            print(f"bq failed: {error}", file=sys.stderr)
            return 5

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
