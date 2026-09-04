"""Offline tests for the function's request handling. These make no network calls.

The whole request path is exercised here against a fake storage writer, which
is why the deployed function needs no local dependencies to be testable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
from datetime import datetime, timezone

from xdrip2gcp.function_source import core_module

core = core_module()

# Deliberately cheap scrypt factors: these tests verify the mechanism, not the
# work factor, and the real ones come from config.
TEST_N, TEST_R, TEST_P, TEST_DKLEN = 1024, 8, 1, 32
PASSWORD = "correct-horse-battery-staple"
SALT = bytes(range(16))


def secret_payload(password: str = PASSWORD) -> dict:
    return core.build_secret_payload(
        password, salt=SALT, n=TEST_N, r=TEST_R, p=TEST_P, dklen=TEST_DKLEN
    )


class FakeStorage:
    """Stands in for Cloud Storage, including its create-only semantics."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.calls: list[str] = []

    def write(self, path: str, data: bytes, content_type: str) -> bool:
        self.calls.append(path)
        if path in self.objects:
            # Mirrors `if_generation_match=0`: an existing object is not
            # overwritten, and the caller is told it was already there.
            return False
        self.objects[path] = data
        self.content_types[path] = content_type
        return True


def make_handler(storage: FakeStorage, password: str = PASSWORD, **kwargs) -> "core.Handler":
    defaults = {
        "secret_payload": secret_payload(password),
        "object_prefix": "cgm-data",
        "writer": storage.write,
        "bucket_name": "test-bucket",
        "now": lambda: datetime(2026, 9, 4, 18, 30, tzinfo=timezone.utc),
    }
    defaults.update(kwargs)
    return core.Handler(**defaults)


def api_secret_header(password: str = PASSWORD) -> dict[str, str]:
    """The header a real Nightscout client sends."""
    return {"api-secret": core.sha1_hex(password)}


class CredentialFormatTests(unittest.TestCase):
    def test_sha1_hex_matches_the_nightscout_protocol(self) -> None:
        # Nightscout clients send the SHA-1 hex digest of the API secret.
        expected = hashlib.sha1(PASSWORD.encode("utf-8")).hexdigest()
        self.assertEqual(core.sha1_hex(PASSWORD), expected)
        self.assertEqual(len(expected), 40)

    def test_existing_digest_passes_through(self) -> None:
        digest = core.sha1_hex(PASSWORD)
        self.assertEqual(core.normalize_credential(digest), digest)
        self.assertEqual(core.normalize_credential(digest.upper()), digest)
        self.assertEqual(core.normalize_credential(f"  {digest}  "), digest)

    def test_plaintext_is_hashed(self) -> None:
        self.assertEqual(core.normalize_credential(PASSWORD), core.sha1_hex(PASSWORD))

    def test_a_40_character_non_hex_string_is_treated_as_plaintext(self) -> None:
        password = "z" * 40
        self.assertEqual(core.normalize_credential(password), core.sha1_hex(password))


class CredentialExtractionTests(unittest.TestCase):
    def test_reads_the_api_secret_header(self) -> None:
        request = core.Request.create("POST", "/api/v1/entries", headers=api_secret_header())
        self.assertEqual(core.extract_credential(request), core.sha1_hex(PASSWORD))

    def test_header_name_is_case_insensitive(self) -> None:
        request = core.Request.create("POST", "/api/v1/entries", headers={"API-SECRET": "abc"})
        self.assertEqual(core.extract_credential(request), "abc")

    def test_reads_basic_auth_password(self) -> None:
        encoded = base64.b64encode(f"user:{PASSWORD}".encode()).decode()
        request = core.Request.create("POST", "/api/v1/entries", headers={"authorization": f"Basic {encoded}"})
        self.assertEqual(core.extract_credential(request), PASSWORD)

    def test_reads_basic_auth_username_when_there_is_no_password(self) -> None:
        # xDrip is configured with https://password@host/api/v1/, so the
        # password can land in the username half.
        encoded = base64.b64encode(f"{PASSWORD}:".encode()).decode()
        request = core.Request.create("POST", "/api/v1/entries", headers={"authorization": f"Basic {encoded}"})
        self.assertEqual(core.extract_credential(request), PASSWORD)

    def test_reads_query_parameters(self) -> None:
        for parameter in ("secret", "token"):
            with self.subTest(parameter=parameter):
                request = core.Request.create("POST", "/api/v1/entries", query={parameter: PASSWORD})
                self.assertEqual(core.extract_credential(request), PASSWORD)

    def test_header_wins_over_query_parameter(self) -> None:
        request = core.Request.create(
            "POST", "/api/v1/entries", headers={"api-secret": "from-header"}, query={"secret": "from-query"}
        )
        self.assertEqual(core.extract_credential(request), "from-header")

    def test_missing_credential_is_none(self) -> None:
        self.assertIsNone(core.extract_credential(core.Request.create("POST", "/api/v1/entries")))

    def test_malformed_basic_auth_is_ignored(self) -> None:
        request = core.Request.create("POST", "/api/v1/entries", headers={"authorization": "Basic not-base64!"})
        self.assertIsNone(core.extract_credential(request))


class SecretPayloadTests(unittest.TestCase):
    def test_payload_records_its_own_parameters(self) -> None:
        payload = secret_payload()
        self.assertEqual(payload["algorithm"], "scrypt")
        self.assertEqual(payload["credential_hash"], "sha1")
        self.assertEqual((payload["n"], payload["r"], payload["p"]), (TEST_N, TEST_R, TEST_P))
        self.assertEqual(payload["salt"], SALT.hex())

    def test_payload_contains_neither_password_nor_wire_credential(self) -> None:
        serialized = json.dumps(secret_payload())
        self.assertNotIn(PASSWORD, serialized)
        self.assertNotIn(core.sha1_hex(PASSWORD), serialized)

    def test_correct_password_verifies_in_either_form(self) -> None:
        payload = secret_payload()
        self.assertTrue(core.verify_credential(core.sha1_hex(PASSWORD), payload))
        self.assertTrue(core.verify_credential(PASSWORD, payload))

    def test_wrong_password_is_rejected(self) -> None:
        payload = secret_payload()
        self.assertFalse(core.verify_credential("wrong-password", payload))
        self.assertFalse(core.verify_credential(core.sha1_hex("wrong-password"), payload))

    def test_salt_changes_the_digest(self) -> None:
        other = core.build_secret_payload(PASSWORD, salt=b"\xff" * 16, n=TEST_N, r=TEST_R, p=TEST_P)
        self.assertNotEqual(secret_payload()["digest"], other["digest"])

    def test_round_trip_through_json(self) -> None:
        payload = core.load_secret_payload(json.dumps(secret_payload()))
        self.assertTrue(core.verify_credential(PASSWORD, payload))

    def test_unusable_payloads_are_rejected(self) -> None:
        cases = {
            "not json": "{{{",
            "not an object": "[1, 2, 3]",
            "wrong algorithm": json.dumps({"algorithm": "md5", "salt": "00", "digest": "x"}),
            "missing digest": json.dumps({"algorithm": "scrypt", "salt": "00", "n": 2, "r": 8, "p": 1, "dklen": 32}),
        }
        for reason, raw in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(core.SecretPayloadError):
                    core.load_secret_payload(raw)


class PayloadParsingTests(unittest.TestCase):
    def test_single_object_becomes_a_one_document_list(self) -> None:
        documents = core.parse_documents(b'{"sgv": 120}', 1024)
        self.assertEqual(documents, [{"sgv": 120}])

    def test_array_is_preserved(self) -> None:
        documents = core.parse_documents(b'[{"sgv": 120}, {"sgv": 121}]', 1024)
        self.assertEqual(len(documents), 2)

    def test_rejects_empty_invalid_and_non_object_bodies(self) -> None:
        for reason, body in {
            "empty": b"",
            "whitespace": b"   \n",
            "invalid json": b"{not json}",
            "empty array": b"[]",
            "array of scalars": b"[1, 2, 3]",
        }.items():
            with self.subTest(reason=reason):
                with self.assertRaises(core.PayloadError) as caught:
                    core.parse_documents(body, 1024)
                self.assertEqual(caught.exception.status, 400)

    def test_oversized_body_is_rejected_before_parsing(self) -> None:
        with self.assertRaises(core.PayloadError) as caught:
            core.parse_documents(b"x" * 100, 10)
        self.assertEqual(caught.exception.status, 413)


class SerializationTests(unittest.TestCase):
    def test_ndjson_is_one_document_per_line(self) -> None:
        data = core.to_ndjson([{"a": 1}, {"b": 2}])
        self.assertEqual(data, b'{"a":1}\n{"b":2}\n')

    def test_key_order_does_not_affect_the_bytes(self) -> None:
        # This is what makes a client's retry resolve to the same object.
        first = core.to_ndjson([{"sgv": 120, "date": 1, "device": "x"}])
        second = core.to_ndjson([{"device": "x", "date": 1, "sgv": 120}])
        self.assertEqual(first, second)

    def test_object_path_is_hive_partitioned(self) -> None:
        when = datetime(2026, 9, 4, 23, 59, tzinfo=timezone.utc)
        path = core.object_path("cgm-data", "entries", when, b"payload")
        self.assertTrue(path.startswith("cgm-data/collection=entries/dt=2026-09-04/"))
        self.assertTrue(path.endswith(".ndjson"))

    def test_object_path_is_content_addressed(self) -> None:
        when = datetime(2026, 9, 4, tzinfo=timezone.utc)
        same = core.object_path("cgm-data", "entries", when, b"payload")
        again = core.object_path("cgm-data", "entries", when, b"payload")
        different = core.object_path("cgm-data", "entries", when, b"other payload")
        self.assertEqual(same, again)
        self.assertNotEqual(same, different)

    def test_collection_and_day_partition_separately(self) -> None:
        payload = b"payload"
        entries = core.object_path("p", "entries", datetime(2026, 9, 4, tzinfo=timezone.utc), payload)
        treatments = core.object_path("p", "treatments", datetime(2026, 9, 4, tzinfo=timezone.utc), payload)
        next_day = core.object_path("p", "entries", datetime(2026, 9, 5, tzinfo=timezone.utc), payload)
        self.assertNotEqual(entries, treatments)
        self.assertNotEqual(entries, next_day)


class RoutingTests(unittest.TestCase):
    def test_finds_the_api_root_in_either_url_style(self) -> None:
        self.assertEqual(core.api_suffix("/api/v1/entries"), "entries")
        # cloudfunctions.net URLs put the function name ahead of the path.
        self.assertEqual(core.api_suffix("/xdrip2gcp-nightscout-test/api/v1/entries"), "entries")
        self.assertEqual(core.api_suffix("/api/v1/entries/"), "entries")

    def test_paths_outside_the_api_root_are_unrecognized(self) -> None:
        self.assertIsNone(core.api_suffix("/"))
        self.assertIsNone(core.api_suffix("/api/v3/entries"))


class HandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = FakeStorage()
        self.handler = make_handler(self.storage)

    def post(self, path: str, body: bytes, headers: dict | None = None, query: dict | None = None):
        return self.handler.handle(
            core.Request.create("POST", path, headers=headers if headers is not None else api_secret_header(), query=query, body=body)
        )

    def test_status_needs_no_credential(self) -> None:
        response = self.handler.handle(core.Request.create("GET", "/api/v1/status"))
        self.assertEqual(response.status, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["apiEnabled"])

    def test_status_json_suffix_also_works(self) -> None:
        self.assertEqual(self.handler.handle(core.Request.create("GET", "/api/v1/status.json")).status, 200)

    def test_auth_check_endpoint_requires_a_credential(self) -> None:
        authorized = self.handler.handle(
            core.Request.create("GET", "/api/v1/experiments/test", headers=api_secret_header())
        )
        self.assertEqual(authorized.status, 200)
        rejected = self.handler.handle(core.Request.create("GET", "/api/v1/experiments/test"))
        self.assertEqual(rejected.status, 401)

    def test_posting_entries_stores_ndjson(self) -> None:
        response = self.post("/api/v1/entries", b'[{"sgv": 120, "date": 1757000000000}]')
        self.assertEqual(response.status, 200)

        path = response.headers["x-xdrip2gcp-object"]
        self.assertEqual(response.headers["x-xdrip2gcp-stored"], "new")
        self.assertEqual(response.headers["x-xdrip2gcp-documents"], "1")
        self.assertIn("collection=entries", path)
        self.assertEqual(self.storage.content_types[path], core.NDJSON_CONTENT_TYPE)
        self.assertEqual(json.loads(self.storage.objects[path]), {"date": 1757000000000, "sgv": 120})

    def test_response_body_echoes_the_documents(self) -> None:
        # A real Nightscout returns the stored documents, so clients see
        # nothing unexpected; our metadata travels in headers instead.
        response = self.post("/api/v1/entries", b'[{"sgv": 120}]')
        self.assertEqual(response.json(), [{"sgv": 120}])

    def test_all_writable_collections_are_accepted(self) -> None:
        for collection in core.WRITABLE_COLLECTIONS:
            with self.subTest(collection=collection):
                response = self.post(f"/api/v1/{collection}", b'{"device": "xDrip"}')
                self.assertEqual(response.status, 200)
                self.assertIn(f"collection={collection}", response.headers["x-xdrip2gcp-object"])

    def test_json_suffixed_collection_paths_work(self) -> None:
        response = self.post("/api/v1/entries.json", b'{"sgv": 120}')
        self.assertEqual(response.status, 200)
        self.assertIn("collection=entries", response.headers["x-xdrip2gcp-object"])

    def test_resending_the_same_batch_does_not_duplicate(self) -> None:
        body = b'[{"sgv": 120, "date": 1757000000000}]'
        first = self.post("/api/v1/entries", body)
        second = self.post("/api/v1/entries", body)

        self.assertEqual(first.headers["x-xdrip2gcp-stored"], "new")
        self.assertEqual(second.headers["x-xdrip2gcp-stored"], "duplicate")
        self.assertEqual(first.headers["x-xdrip2gcp-object"], second.headers["x-xdrip2gcp-object"])
        self.assertEqual(len(self.storage.objects), 1)

    def test_reordered_keys_resolve_to_the_same_object(self) -> None:
        self.post("/api/v1/entries", b'[{"sgv": 120, "date": 1}]')
        self.post("/api/v1/entries", b'[{"date": 1, "sgv": 120}]')
        self.assertEqual(len(self.storage.objects), 1)

    def test_credential_from_query_parameter_is_accepted(self) -> None:
        response = self.post(
            "/api/v1/entries", b'{"sgv": 120}', headers={}, query={"secret": core.sha1_hex(PASSWORD)}
        )
        self.assertEqual(response.status, 200)

    def test_missing_and_wrong_credentials_are_rejected(self) -> None:
        for reason, headers in {
            "no credential": {},
            "wrong password": {"api-secret": core.sha1_hex("nope")},
            "plaintext of wrong password": {"api-secret": "nope"},
        }.items():
            with self.subTest(reason=reason):
                response = self.post("/api/v1/entries", b'{"sgv": 120}', headers=headers)
                self.assertEqual(response.status, 401)
                self.assertEqual(self.storage.objects, {}, "nothing may be written unauthenticated")

    def test_authentication_is_checked_before_the_payload(self) -> None:
        response = self.post("/api/v1/entries", b"not json at all", headers={})
        self.assertEqual(response.status, 401)

    def test_bad_payloads_are_rejected_after_authentication(self) -> None:
        self.assertEqual(self.post("/api/v1/entries", b"{not json}").status, 400)
        self.assertEqual(self.post("/api/v1/entries", b"").status, 400)

    def test_oversized_payload_is_rejected(self) -> None:
        handler = make_handler(self.storage, max_request_bytes=32)
        request = core.Request.create(
            "POST", "/api/v1/entries", headers=api_secret_header(), body=b'[{"sgv": 120}]' + b" " * 100
        )
        self.assertEqual(handler.handle(request).status, 413)

    def test_wrong_method_is_405(self) -> None:
        get_entries = self.handler.handle(
            core.Request.create("GET", "/api/v1/entries", headers=api_secret_header())
        )
        self.assertEqual(get_entries.status, 405)
        post_status = self.handler.handle(core.Request.create("POST", "/api/v1/status"))
        self.assertEqual(post_status.status, 405)

    def test_unknown_paths_are_404_and_list_the_endpoints(self) -> None:
        for path in ("/", "/api/v1/unknown", "/api/v3/entries"):
            with self.subTest(path=path):
                response = self.handler.handle(core.Request.create("GET", path))
                self.assertEqual(response.status, 404)
                self.assertIn("endpoints", response.json())

    def test_error_responses_never_include_the_credential(self) -> None:
        response = self.post("/api/v1/entries", b'{"sgv": 120}', headers={"api-secret": core.sha1_hex("nope")})
        self.assertNotIn(PASSWORD.encode(), response.body)
        self.assertNotIn(core.sha1_hex("nope").encode(), response.body)
