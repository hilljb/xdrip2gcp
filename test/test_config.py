"""Offline tests for configuration loading. These make no network calls."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from xdrip2gcp import config as config_module
from xdrip2gcp.config import ConfigError, ensure_password, load_config, validate_bucket_name

SHARED_TOML = """
[project]
id = "shared-project"

[bucket]
name = ""
base_name = "xdrip2gcp-test"
name_suffix = ""
location = "us-central1"
storage_class = "STANDARD"
uniform_bucket_level_access = true
public_access_prevention = true
lifecycle_age_days = 3

[objects]
test_prefix = "test-data"
data_prefix = "cgm-data"

[test_data]
record_count = 5
random_seed = 1234
text_object = "hello.txt"
json_object = "sample.json"

[gcloud]
cloudsdk_python = ""
timeout_seconds = 120

[gcp]
services = ["run.googleapis.com"]

[service_accounts]
runtime_id = "fn-runtime"
build_id = "fn-build"

[function]
name = "test-function"
region = ""
runtime = "python314"

[auth]
secret_id = "test-secret"
password = ""
password_bytes = 24
"""


class ConfigLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._workdir.cleanup)
        root = Path(self._workdir.name)
        self.shared_path = root / "config.toml"
        self.local_path = root / "config.local.toml"
        self.shared_path.write_text(SHARED_TOML)

        # Environment overrides are global state; clear them for every test.
        for env_name in config_module.ENV_OVERRIDES:
            if env_name in os.environ:
                self.addCleanup(os.environ.__setitem__, env_name, os.environ[env_name])
                del os.environ[env_name]

    def load(self, **kwargs):
        return load_config(self.shared_path, self.local_path, **kwargs)

    def test_reads_shared_defaults(self) -> None:
        config = self.load()
        self.assertEqual(config.project_id, "shared-project")
        self.assertEqual(config.location, "us-central1")
        self.assertEqual(config.lifecycle_age_days, 3)
        self.assertEqual(config.test_prefix, "test-data")
        self.assertTrue(config.public_access_prevention)

    def test_local_file_overrides_shared_values(self) -> None:
        self.local_path.write_text('[project]\nid = "local-project"\n\n[bucket]\nname_suffix = "abc123"\n')
        config = self.load()
        self.assertEqual(config.project_id, "local-project")
        self.assertEqual(config.bucket_name, "xdrip2gcp-test-abc123")

    def test_environment_overrides_both_files(self) -> None:
        self.local_path.write_text('[project]\nid = "local-project"\n\n[bucket]\nname_suffix = "abc123"\n')
        os.environ["XDRIP2GCP_PROJECT_ID"] = "env-project"
        os.environ["XDRIP2GCP_BUCKET_NAME"] = "env-bucket-name"
        self.addCleanup(os.environ.pop, "XDRIP2GCP_PROJECT_ID", None)
        self.addCleanup(os.environ.pop, "XDRIP2GCP_BUCKET_NAME", None)

        config = self.load()
        self.assertEqual(config.project_id, "env-project")
        self.assertEqual(config.bucket_name, "env-bucket-name")

    def test_generated_suffix_is_persisted_and_reused(self) -> None:
        first = self.load()
        self.assertTrue(self.local_path.exists(), "first load should record the suffix")
        second = self.load()
        self.assertEqual(
            first.bucket_name,
            second.bucket_name,
            "a second load must resolve to the same bucket, not generate a new one",
        )

    def test_existing_local_suffix_is_never_overwritten(self) -> None:
        self.local_path.write_text('[bucket]\nname_suffix = "keepme"\n')
        config = self.load()
        self.assertEqual(config.bucket_name, "xdrip2gcp-test-keepme")
        self.assertIn("keepme", self.local_path.read_text())

    def test_suffix_generation_can_be_refused(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(allow_generation=False)
        self.assertFalse(
            self.local_path.exists(),
            "refusing to generate a suffix must not write a local config file",
        )

    def test_explicit_name_wins_over_base_and_suffix(self) -> None:
        self.local_path.write_text('[bucket]\nname = "explicit-bucket"\nname_suffix = "ignored"\n')
        self.assertEqual(self.load().bucket_name, "explicit-bucket")

    def test_missing_project_id_is_rejected(self) -> None:
        self.shared_path.write_text(SHARED_TOML.replace('id = "shared-project"', 'id = ""'))
        with self.assertRaises(ConfigError):
            self.load()

    def test_negative_lifecycle_is_rejected(self) -> None:
        self.shared_path.write_text(SHARED_TOML.replace("lifecycle_age_days = 3", "lifecycle_age_days = -1"))
        with self.assertRaises(ConfigError):
            self.load()

    def test_missing_shared_config_is_rejected(self) -> None:
        self.shared_path.unlink()
        with self.assertRaises(ConfigError):
            self.load()

    def test_bucket_uri_and_object_paths(self) -> None:
        self.local_path.write_text('[bucket]\nname = "explicit-bucket"\n')
        config = self.load()
        self.assertEqual(config.bucket_uri, "gs://explicit-bucket")
        self.assertEqual(
            config.test_object_uri("hello.txt"),
            "gs://explicit-bucket/test-data/hello.txt",
        )


class Stage3ConfigTests(unittest.TestCase):
    """Function, identity and credential settings."""

    def setUp(self) -> None:
        self._workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._workdir.cleanup)
        root = Path(self._workdir.name)
        self.shared_path = root / "config.toml"
        self.local_path = root / "config.local.toml"
        self.shared_path.write_text(SHARED_TOML)
        self.local_path.write_text('[bucket]\nname = "explicit-bucket"\n')

    def load(self):
        return load_config(self.shared_path, self.local_path)

    def test_function_region_falls_back_to_the_bucket_location(self) -> None:
        # Keeping the function beside its bucket avoids cross-region egress.
        config = self.load()
        self.assertEqual(config.function.region, "")
        self.assertEqual(config.function_region, config.location)

    def test_explicit_function_region_wins(self) -> None:
        self.shared_path.write_text(SHARED_TOML.replace('region = ""', 'region = "us-east1"'))
        self.assertEqual(self.load().function_region, "us-east1")

    def test_service_account_emails_are_derived_from_the_project(self) -> None:
        config = self.load()
        self.assertEqual(config.runtime_service_account, "fn-runtime@shared-project.iam.gserviceaccount.com")
        self.assertEqual(config.build_service_account, "fn-build@shared-project.iam.gserviceaccount.com")

    def test_secret_resource_path(self) -> None:
        self.assertEqual(self.load().secret_resource, "projects/shared-project/secrets/test-secret")

    def test_password_is_generated_once_and_reused(self) -> None:
        first, generated = ensure_password(self.load(), self.local_path)
        self.assertTrue(generated)
        self.assertTrue(first.auth.password)

        second, generated_again = ensure_password(self.load(), self.local_path)
        self.assertFalse(generated_again, "a recorded password must not be regenerated")
        self.assertEqual(first.auth.password, second.auth.password)

    def test_generated_password_is_safe_in_an_xdrip_url(self) -> None:
        # xDrip takes https://password@host/api/v1/, so these would break parsing.
        config, _ = ensure_password(self.load(), self.local_path)
        for character in "@/:?#":
            self.assertNotIn(character, config.auth.password)

    def test_persisting_a_value_preserves_existing_local_settings(self) -> None:
        self.local_path.write_text(
            "# my notes\n[bucket]\nname = \"explicit-bucket\"\n\n[project]\nid = \"local-project\"\n"
        )
        ensure_password(self.load(), self.local_path)

        text = self.local_path.read_text()
        self.assertIn("# my notes", text)
        self.assertIn('name = "explicit-bucket"', text)
        self.assertIn('id = "local-project"', text)
        self.assertIn("[auth]", text)
        self.assertTrue(self.load().auth.password, "the new value must be readable back")

    def test_persisting_into_an_existing_section_does_not_duplicate_it(self) -> None:
        self.local_path.write_text('[auth]\nsecret_id = "custom-secret"\n')
        ensure_password(self.load(), self.local_path)

        text = self.local_path.read_text()
        self.assertEqual(text.count("[auth]"), 1)
        config = self.load()
        self.assertEqual(config.auth.secret_id, "custom-secret")
        self.assertTrue(config.auth.password)


class BucketNameValidationTests(unittest.TestCase):
    def test_accepts_valid_names(self) -> None:
        for name in ("xdrip2gcp-test-c4278d", "abc", "a-1"):
            with self.subTest(name=name):
                validate_bucket_name(name)

    def test_rejects_invalid_names(self) -> None:
        invalid = {
            "too short": "ab",
            "too long": "a" * 64,
            "uppercase": "Xdrip2gcp-Test",
            "underscore": "xdrip2gcp_test",
            "dot": "xdrip2gcp.test",
            "leading dash": "-xdrip2gcp",
            "trailing dash": "xdrip2gcp-",
            "goog prefix": "googtest-bucket",
            "contains google": "my-google-bucket",
        }
        for reason, name in invalid.items():
            with self.subTest(reason=reason):
                with self.assertRaises(ConfigError):
                    validate_bucket_name(name)


class CloudSdkPythonTests(unittest.TestCase):
    def test_missing_configured_interpreter_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            config_module.resolve_cloudsdk_python("/nonexistent/python")

    def test_autodetected_interpreter_is_modern_enough(self) -> None:
        import subprocess

        interpreter = config_module.resolve_cloudsdk_python("")
        version = subprocess.run(
            [interpreter, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        major, minor = (int(part) for part in version.split("."))
        self.assertGreaterEqual(
            (major, minor),
            config_module.MIN_GCLOUD_PYTHON,
            f"gcloud needs Python >= {config_module.MIN_GCLOUD_PYTHON}, resolved {version}",
        )
