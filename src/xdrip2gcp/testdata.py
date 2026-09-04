"""Generic test payloads for proving bucket writes and reads work.

Generation is seeded from config, so the bytes are byte-for-byte identical on
every run. That is what lets `upload_bytes` recognize an unchanged object and
skip the transfer, and it lets tests assert on exact content after a round trip.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TestPayload:
    """One object's worth of test data."""

    object_name: str
    content: bytes


def text_payload(config: Config) -> TestPayload:
    name = str(config.test_data.get("text_object", "hello.txt"))
    body = (
        "xdrip2gcp bucket write test\n"
        f"schema_version={SCHEMA_VERSION}\n"
        f"project={config.project_id}\n"
        f"bucket={config.bucket_name}\n"
    )
    return TestPayload(name, body.encode("utf-8"))


def json_payload(config: Config) -> TestPayload:
    name = str(config.test_data.get("json_object", "sample.json"))
    count = int(config.test_data.get("record_count", 5))
    seed = int(config.test_data.get("random_seed", 0))
    rng = random.Random(seed)

    document = {
        "schema_version": SCHEMA_VERSION,
        "source": "xdrip2gcp-test-data",
        "record_count": count,
        "records": [
            {
                "id": index,
                "label": f"record-{index:03d}",
                "value": round(rng.uniform(0.0, 100.0), 4),
            }
            for index in range(count)
        ],
    }
    body = json.dumps(document, indent=2, sort_keys=True) + "\n"
    return TestPayload(name, body.encode("utf-8"))


def all_payloads(config: Config) -> list[TestPayload]:
    return [text_payload(config), json_payload(config)]


# Nightscout's direction vocabulary, as xDrip sends it.
DIRECTIONS = ("Flat", "FortyFiveUp", "SingleUp", "FortyFiveDown", "SingleDown")


def nightscout_entries(config: Config) -> list[dict]:
    """Generate CGM readings shaped like xDrip's `/api/v1/entries` uploads.

    Timestamps come from a fixed base rather than the current clock, so the
    serialized batch is byte-identical between runs. That is what lets a test
    predict the content-addressed object name and assert that re-posting the
    same batch is recognized as a duplicate.
    """
    count = int(config.test_data.get("entry_count", 3))
    base_ms = int(config.test_data.get("entry_base_ms", 1757000000000))
    interval_ms = int(config.test_data.get("entry_interval_ms", 300000))
    rng = random.Random(int(config.test_data.get("random_seed", 0)))

    entries = []
    for index in range(count):
        timestamp_ms = base_ms + index * interval_ms
        entries.append(
            {
                "device": "xDrip-xdrip2gcp-test",
                "date": timestamp_ms,
                "dateString": _iso_utc(timestamp_ms),
                "sgv": rng.randint(70, 180),
                "direction": DIRECTIONS[index % len(DIRECTIONS)],
                "type": "sgv",
                "noise": 1,
                "sysTime": _iso_utc(timestamp_ms),
            }
        )
    return entries


def _iso_utc(timestamp_ms: int) -> str:
    moment = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def object_path(config: Config, payload: TestPayload) -> str:
    """Full in-bucket path for a payload, under the configured test prefix."""
    return f"{config.test_prefix}/{payload.object_name}"
