"""Generic test payloads for proving bucket writes and reads work.

Generation is seeded from config, so the bytes are byte-for-byte identical on
every run. That is what lets `upload_bytes` recognize an unchanged object and
skip the transfer, and it lets tests assert on exact content after a round trip.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

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


def object_path(config: Config, payload: TestPayload) -> str:
    """Full in-bucket path for a payload, under the configured test prefix."""
    return f"{config.test_prefix}/{payload.object_name}"
