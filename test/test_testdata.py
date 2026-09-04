"""Offline tests for test-data generation. These make no network calls."""

from __future__ import annotations

import dataclasses
import json
import unittest
from datetime import datetime

from xdrip2gcp import testdata
from xdrip2gcp.config import Config

CONFIG = Config(
    project_id="unit-test-project",
    bucket_name="unit-test-bucket",
    location="us-central1",
    storage_class="STANDARD",
    uniform_bucket_level_access=True,
    public_access_prevention=True,
    lifecycle_age_days=3,
    test_prefix="test-data",
    data_prefix="cgm-data",
    test_data={
        "record_count": 4,
        "random_seed": 99,
        "text_object": "hello.txt",
        "json_object": "sample.json",
        "entry_count": 3,
        "entry_base_ms": 1757000000000,
        "entry_interval_ms": 300000,
    },
    cloudsdk_python="/usr/bin/python3",
)


class TestDataGenerationTests(unittest.TestCase):
    def test_payloads_cover_both_objects(self) -> None:
        names = [payload.object_name for payload in testdata.all_payloads(CONFIG)]
        self.assertEqual(names, ["hello.txt", "sample.json"])

    def test_generation_is_deterministic(self) -> None:
        first = testdata.all_payloads(CONFIG)
        second = testdata.all_payloads(CONFIG)
        self.assertEqual(
            [payload.content for payload in first],
            [payload.content for payload in second],
            "identical config must produce identical bytes, or uploads stop being no-ops",
        )

    def test_seed_changes_json_content(self) -> None:
        other = dataclasses.replace(CONFIG, test_data={**CONFIG.test_data, "random_seed": 100})
        self.assertNotEqual(
            testdata.json_payload(CONFIG).content,
            testdata.json_payload(other).content,
        )

    def test_json_payload_structure(self) -> None:
        document = json.loads(testdata.json_payload(CONFIG).content)
        self.assertEqual(document["schema_version"], testdata.SCHEMA_VERSION)
        self.assertEqual(document["record_count"], 4)
        self.assertEqual(len(document["records"]), 4)
        self.assertEqual([record["id"] for record in document["records"]], [0, 1, 2, 3])
        for record in document["records"]:
            self.assertGreaterEqual(record["value"], 0.0)
            self.assertLessEqual(record["value"], 100.0)

    def test_text_payload_names_its_target(self) -> None:
        content = testdata.text_payload(CONFIG).content.decode("utf-8")
        self.assertIn("project=unit-test-project", content)
        self.assertIn("bucket=unit-test-bucket", content)

    def test_object_paths_sit_under_the_test_prefix(self) -> None:
        for payload in testdata.all_payloads(CONFIG):
            path = testdata.object_path(CONFIG, payload)
            self.assertTrue(path.startswith("test-data/"), path)


class NightscoutEntryTests(unittest.TestCase):
    """CGM readings shaped the way xDrip uploads them."""

    def test_entries_carry_the_nightscout_fields(self) -> None:
        for entry in testdata.nightscout_entries(CONFIG):
            self.assertEqual(entry["type"], "sgv")
            self.assertIn("date", entry)
            self.assertIn("dateString", entry)
            self.assertIn("direction", entry)
            self.assertIn("device", entry)
            self.assertGreaterEqual(entry["sgv"], 70)
            self.assertLessEqual(entry["sgv"], 180)

    def test_entry_count_follows_config(self) -> None:
        self.assertEqual(len(testdata.nightscout_entries(CONFIG)), 3)

    def test_generation_is_deterministic(self) -> None:
        # A byte-identical batch is what makes the function's duplicate
        # detection testable and the object name predictable.
        self.assertEqual(testdata.nightscout_entries(CONFIG), testdata.nightscout_entries(CONFIG))

    def test_timestamps_are_evenly_spaced_from_the_configured_base(self) -> None:
        entries = testdata.nightscout_entries(CONFIG)
        self.assertEqual(entries[0]["date"], 1757000000000)
        gaps = {second["date"] - first["date"] for first, second in zip(entries, entries[1:])}
        self.assertEqual(gaps, {300000})

    def test_date_string_matches_the_epoch_timestamp(self) -> None:
        entry = testdata.nightscout_entries(CONFIG)[0]
        parsed = datetime.fromisoformat(entry["dateString"].replace("Z", "+00:00"))
        self.assertEqual(int(parsed.timestamp() * 1000), entry["date"])
