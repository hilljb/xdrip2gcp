"""Live tests against the real Cloud Storage test bucket.

These call the gcloud CLI, so they need `gcloud auth login` and a bucket
created by `python src/create_bucket.py`. Without those they skip rather than
fail. Objects are written under a per-run scratch prefix and deleted again, so
the suite leaves the bucket as it found it; the bucket's lifecycle rule cleans
up anything a crashed run abandons.
"""

from __future__ import annotations

import dataclasses
import json
import uuid

from xdrip2gcp import bucket, testdata

from .support import LiveGcpTestCase


class BucketStateTests(LiveGcpTestCase):
    """The bucket should match what the configuration asked for."""

    def test_bucket_matches_configuration(self) -> None:
        summary = bucket.bucket_summary(self.config)
        self.assertTrue(summary["exists"])
        self.assertEqual(summary["name"], self.config.bucket_name)
        self.assertEqual(summary["location"], self.config.location.lower())
        self.assertEqual(summary["storage_class"], self.config.storage_class)

    def test_safety_settings_are_enforced(self) -> None:
        summary = bucket.bucket_summary(self.config)
        if self.config.uniform_bucket_level_access:
            self.assertTrue(summary["uniform_bucket_level_access"])
        if self.config.public_access_prevention:
            self.assertEqual(summary["public_access_prevention"], "enforced")

    def test_lifecycle_rule_matches_configuration(self) -> None:
        summary = bucket.bucket_summary(self.config)
        expected = [self.config.lifecycle_age_days] if self.config.lifecycle_age_days > 0 else []
        self.assertEqual(summary["lifecycle_delete_ages"], expected)

    def test_creating_an_existing_bucket_is_a_noop(self) -> None:
        result = bucket.create_bucket(self.config)
        self.assertFalse(result.changed, f"expected no-op, got: {result.detail}")

    def test_reapplying_lifecycle_is_a_noop(self) -> None:
        result = bucket.apply_lifecycle(self.config)
        self.assertFalse(result.changed, f"expected no-op, got: {result.detail}")

    def test_missing_bucket_is_reported_not_raised(self) -> None:
        absent = dataclasses.replace(
            self.config, bucket_name=f"xdrip2gcp-absent-{uuid.uuid4().hex[:12]}"
        )
        self.assertIsNone(bucket.describe_bucket(absent))
        self.assertFalse(bucket.bucket_exists(absent))


class ObjectRoundTripTests(LiveGcpTestCase):
    """Writing, reading, listing and deleting objects, all idempotently."""

    def setUp(self) -> None:
        # A unique prefix per test keeps concurrent or interrupted runs from
        # interfering with each other.
        self.prefix = f"{self.config.test_prefix}/scratch/{uuid.uuid4().hex[:12]}"
        self._written: list[str] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for path in self._written:
            bucket.delete_object(self.config, path)

    def _write(self, name: str, payload: bytes) -> str:
        path = f"{self.prefix}/{name}"
        self._written.append(path)
        bucket.upload_bytes(self.config, path, payload)
        return path

    def test_upload_then_download_preserves_bytes(self) -> None:
        payload = b"xdrip2gcp round trip\n\x00\x01binary-safe\xff\n"
        path = self._write("round-trip.bin", payload)
        self.assertEqual(bucket.download_bytes(self.config, path), payload)

    def test_first_upload_changes_and_second_is_a_noop(self) -> None:
        payload = b"identical bytes\n"
        path = f"{self.prefix}/idempotent.txt"
        self._written.append(path)

        first = bucket.upload_bytes(self.config, path, payload)
        self.assertTrue(first.changed, f"first upload should change: {first.detail}")

        second = bucket.upload_bytes(self.config, path, payload)
        self.assertFalse(second.changed, f"unchanged bytes should skip the upload: {second.detail}")

    def test_changed_bytes_are_uploaded_again(self) -> None:
        path = f"{self.prefix}/mutable.txt"
        self._written.append(path)

        bucket.upload_bytes(self.config, path, b"version one\n")
        result = bucket.upload_bytes(self.config, path, b"version two\n")
        self.assertTrue(result.changed)
        self.assertEqual(bucket.download_bytes(self.config, path), b"version two\n")

    def test_listing_returns_object_paths_only(self) -> None:
        first = self._write("one.txt", b"one\n")
        second = self._write("two.txt", b"two\n")

        listed = bucket.list_objects(self.config, self.prefix)
        self.assertEqual(listed, sorted([first, second]))
        for path in listed:
            self.assertFalse(path.endswith("/"), f"directory placeholder leaked into listing: {path}")
            self.assertFalse(path.endswith(":"), f"listing header leaked into listing: {path}")

    def test_listing_an_empty_prefix_is_empty_not_an_error(self) -> None:
        self.assertEqual(bucket.list_objects(self.config, f"{self.prefix}/nothing-here"), [])

    def test_describe_missing_object_returns_none(self) -> None:
        self.assertIsNone(bucket.describe_object(self.config, f"{self.prefix}/never-written.txt"))

    def test_delete_is_idempotent(self) -> None:
        path = self._write("deleteme.txt", b"delete me\n")

        first = bucket.delete_object(self.config, path)
        self.assertTrue(first.changed, f"first delete should change: {first.detail}")

        second = bucket.delete_object(self.config, path)
        self.assertFalse(second.changed, f"second delete should be a no-op: {second.detail}")
        self.assertIsNone(bucket.describe_object(self.config, path))


class GeneratedTestDataTests(LiveGcpTestCase):
    """The configured test payloads should survive a trip through the bucket."""

    def setUp(self) -> None:
        self.prefix = f"{self.config.test_prefix}/scratch/{uuid.uuid4().hex[:12]}"
        self.paths: list[str] = []
        self.addCleanup(lambda: [bucket.delete_object(self.config, path) for path in self.paths])

    def test_all_payloads_round_trip(self) -> None:
        for payload in testdata.all_payloads(self.config):
            with self.subTest(object_name=payload.object_name):
                path = f"{self.prefix}/{payload.object_name}"
                self.paths.append(path)
                bucket.upload_bytes(self.config, path, payload.content)
                self.assertEqual(bucket.download_bytes(self.config, path), payload.content)

    def test_json_payload_is_still_valid_json_after_round_trip(self) -> None:
        payload = testdata.json_payload(self.config)
        path = f"{self.prefix}/{payload.object_name}"
        self.paths.append(path)

        bucket.upload_bytes(self.config, path, payload.content)
        document = json.loads(bucket.download_bytes(self.config, path))
        self.assertEqual(document["record_count"], len(document["records"]))
        self.assertEqual(document, json.loads(payload.content))
