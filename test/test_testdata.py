"""Offline tests for test-data generation. These make no network calls."""

from __future__ import annotations

import dataclasses
import json
import unittest

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
