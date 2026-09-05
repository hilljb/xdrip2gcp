"""Live tests against the deployed Cloud Function.

These need `gcloud auth login`, a created bucket, and a deployed function; each
is checked in `LiveFunctionTestCase`, which skips with an explanation rather
than failing when something is missing.

Objects the function writes during these tests are deleted afterwards using the
local gcloud credentials. The bucket's lifecycle rule cleans up anything an
interrupted run abandons.
"""

from __future__ import annotations

import json

from xdrip2gcp import bucket, cloudfunction, secretmanager, testdata
from xdrip2gcp.function_source import core_module

from .support import LiveFunctionTestCase, http_request

core = core_module()


class DeployedFunctionTests(LiveFunctionTestCase):
    """The deployment should match the configuration, and re-running is free."""

    def test_function_matches_configuration(self) -> None:
        summary = cloudfunction.summary(self.config)
        self.assertTrue(summary["exists"])
        self.assertEqual(summary["state"], "ACTIVE")
        self.assertEqual(summary["runtime"], self.config.function.runtime)
        self.assertEqual(summary["region"], self.config.function_region)
        self.assertEqual(summary["max_instances"], self.config.function.max_instances)
        self.assertEqual(summary["timeout_seconds"], self.config.function.timeout_seconds)

    def test_runs_as_the_dedicated_least_privilege_identity(self) -> None:
        summary = cloudfunction.summary(self.config)
        self.assertEqual(summary["service_account"], self.config.runtime_service_account)

    def test_environment_points_at_the_configured_bucket(self) -> None:
        environment = cloudfunction.summary(self.config)["environment"]
        self.assertEqual(environment["XDRIP2GCP_BUCKET"], self.config.bucket_name)
        self.assertEqual(environment["XDRIP2GCP_OBJECT_PREFIX"], self.config.data_prefix)

    def test_the_password_is_not_visible_in_the_deployment(self) -> None:
        # The plaintext lives only in config.local.toml; GCP holds a digest.
        summary = json.dumps(cloudfunction.summary(self.config))
        self.assertNotIn(self.password, summary)

    def test_stored_digest_never_contains_a_replayable_credential(self) -> None:
        payload = secretmanager.latest_payload(self.config)
        serialized = json.dumps(payload)
        self.assertNotIn(self.password, serialized)
        self.assertNotIn(core.sha1_hex(self.password), serialized)

    def test_stored_digest_verifies_the_local_password(self) -> None:
        payload = secretmanager.latest_payload(self.config)
        self.assertTrue(core.verify_credential(core.sha1_hex(self.password), payload))
        self.assertFalse(core.verify_credential("wrong-password", payload))

    def test_reapplying_the_credential_is_a_noop(self) -> None:
        for result in secretmanager.ensure_credential(self.config):
            self.assertFalse(result.changed, f"expected no-op, got: {result.detail}")

    def test_redeploying_unchanged_source_is_a_noop(self) -> None:
        result = cloudfunction.deploy(self.config)
        self.assertFalse(result.changed, f"expected no-op, got: {result.detail}")


class EndpointTests(LiveFunctionTestCase):
    """End-to-end behaviour over HTTPS, including what lands in the bucket."""

    def setUp(self) -> None:
        self.created: list[str] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for path in self.created:
            bucket.delete_object(self.config, path)

    def post(self, endpoint: str, documents, headers=None, query: str = ""):
        """POST documents, recording any object written so it can be removed."""
        body = json.dumps(documents).encode("utf-8")
        if headers is None:
            headers = {"api-secret": core.sha1_hex(self.password)}
        merged = {"Content-Type": "application/json", **headers}

        result = http_request(self.api_url(endpoint, query), method="POST", body=body, headers=merged)
        path = result.header("x-xdrip2gcp-object")
        if path and path not in self.created:
            self.created.append(path)
        return result

    # -- authentication ---------------------------------------------------

    def test_status_is_reachable_without_a_credential(self) -> None:
        for endpoint in ("status", "status.json"):
            with self.subTest(endpoint=endpoint):
                result = http_request(self.api_url(endpoint))
                self.assertEqual(result.status, 200)
                self.assertEqual(result.json()["status"], "ok")
                self.assertTrue(result.json()["apiEnabled"])

    def test_auth_check_endpoint_accepts_and_rejects(self) -> None:
        authorized = http_request(
            self.api_url("experiments/test"), headers={"api-secret": core.sha1_hex(self.password)}
        )
        self.assertEqual(authorized.status, 200)
        self.assertEqual(http_request(self.api_url("experiments/test")).status, 401)

    def test_sha1_digest_header_authenticates(self) -> None:
        # Exactly what xDrip sends.
        result = self.post("entries", [{"sgv": 120, "type": "sgv", "date": 1757000001000}])
        self.assertEqual(result.status, 200)

    def test_plaintext_password_header_authenticates(self) -> None:
        result = self.post("entries", [{"sgv": 121, "date": 1757000002000}], headers={"api-secret": self.password})
        self.assertEqual(result.status, 200)

    def test_query_parameter_authenticates(self) -> None:
        result = self.post(
            "entries",
            [{"sgv": 122, "date": 1757000003000}],
            headers={},
            query=f"secret={core.sha1_hex(self.password)}",
        )
        self.assertEqual(result.status, 200)

    def test_basic_auth_authenticates(self) -> None:
        import base64

        encoded = base64.b64encode(f"{self.password}:".encode()).decode()
        result = self.post(
            "entries", [{"sgv": 123, "date": 1757000004000}], headers={"authorization": f"Basic {encoded}"}
        )
        self.assertEqual(result.status, 200)

    def test_wrong_and_missing_credentials_are_rejected(self) -> None:
        for reason, headers in {
            "no credential": {},
            "wrong digest": {"api-secret": core.sha1_hex("not-the-password")},
            "wrong plaintext": {"api-secret": "not-the-password"},
            "empty header": {"api-secret": ""},
        }.items():
            with self.subTest(reason=reason):
                result = self.post("entries", [{"sgv": 400}], headers=headers)
                self.assertEqual(result.status, 401)
                self.assertIsNone(result.header("x-xdrip2gcp-object"), "nothing may be written")

    # -- routing ----------------------------------------------------------

    def test_get_on_a_write_collection_is_405(self) -> None:
        result = http_request(self.api_url("entries"), headers={"api-secret": core.sha1_hex(self.password)})
        self.assertEqual(result.status, 405)

    def test_unknown_paths_are_404(self) -> None:
        for endpoint in ("nonexistent", "profile"):
            with self.subTest(endpoint=endpoint):
                result = http_request(self.api_url(endpoint))
                self.assertEqual(result.status, 404)
                self.assertIn("endpoints", result.json())

    def test_malformed_payload_is_400(self) -> None:
        result = http_request(
            self.api_url("entries"),
            method="POST",
            body=b"{not json}",
            headers={"api-secret": core.sha1_hex(self.password), "Content-Type": "application/json"},
        )
        self.assertEqual(result.status, 400)

    # -- storage ----------------------------------------------------------

    def test_entries_land_in_the_bucket_as_ndjson(self) -> None:
        entries = testdata.nightscout_entries(self.config)
        result = self.post("entries", entries)
        self.assertEqual(result.status, 200)

        path = result.header("x-xdrip2gcp-object")
        self.assertIsNotNone(path)
        self.assertIn(f"{self.config.data_prefix}/collection=entries/dt=", path)
        self.assertEqual(result.header("x-xdrip2gcp-bucket"), self.config.bucket_name)
        self.assertEqual(result.header("x-xdrip2gcp-documents"), str(len(entries)))

        stored = bucket.download_bytes(self.config, path).decode("utf-8")
        lines = [line for line in stored.splitlines() if line]
        self.assertEqual(len(lines), len(entries))
        self.assertEqual([json.loads(line) for line in lines], entries)

    def test_object_name_is_prefixed_with_the_readings_own_timestamp(self) -> None:
        # Lets a bucket listing be read in time order, since the console sorts
        # objects by name.
        entries = testdata.nightscout_entries(self.config)
        result = self.post("entries", entries)

        name = result.header("x-xdrip2gcp-object").rsplit("/", 1)[-1]
        earliest = min(entry["date"] for entry in entries)
        self.assertTrue(name.startswith(f"{earliest}-"), name)

    def test_stored_object_declares_the_ndjson_content_type(self) -> None:
        result = self.post("entries", testdata.nightscout_entries(self.config))
        metadata = bucket.describe_object(self.config, result.header("x-xdrip2gcp-object"))
        self.assertEqual(metadata["content_type"], core.NDJSON_CONTENT_TYPE)

    def test_response_body_echoes_the_documents_like_nightscout(self) -> None:
        entries = testdata.nightscout_entries(self.config)
        self.assertEqual(self.post("entries", entries).json(), entries)

    def test_reposting_the_same_batch_is_recognized_as_a_duplicate(self) -> None:
        entries = testdata.nightscout_entries(self.config)

        first = self.post("entries", entries)
        second = self.post("entries", entries)

        self.assertEqual(first.header("x-xdrip2gcp-stored"), "new")
        self.assertEqual(second.header("x-xdrip2gcp-stored"), "duplicate")
        self.assertEqual(first.header("x-xdrip2gcp-object"), second.header("x-xdrip2gcp-object"))

        # An xDrip retry after an outage must not create a second object.
        listed = bucket.list_objects(self.config, first.header("x-xdrip2gcp-object"))
        self.assertEqual(len(listed), 1)

    def test_each_collection_writes_to_its_own_partition(self) -> None:
        for collection in core.WRITABLE_COLLECTIONS:
            with self.subTest(collection=collection):
                result = self.post(collection, [{"device": "xDrip-xdrip2gcp-test", "collection": collection}])
                self.assertEqual(result.status, 200)
                self.assertIn(f"collection={collection}/", result.header("x-xdrip2gcp-object"))
