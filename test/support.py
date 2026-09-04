"""Shared helpers for the test suite."""

from __future__ import annotations

import unittest

from xdrip2gcp import bucket, gcloud
from xdrip2gcp.config import Config, ConfigError, load_config


def load_test_config() -> tuple[Config | None, str | None]:
    """Load config, returning either a config or the reason it is unavailable."""
    try:
        # Tests never invent a new bucket name; they use the one already
        # recorded by src/create_bucket.py.
        return load_config(allow_suffix_generation=False), None
    except ConfigError as error:
        return None, str(error)


class LiveGcpTestCase(unittest.TestCase):
    """Base class for tests that talk to a real bucket.

    Skips with an explanatory message rather than failing when gcloud is
    missing, unauthenticated, or the bucket has not been created yet, so the
    suite stays runnable on a fresh clone.
    """

    config: Config

    @classmethod
    def setUpClass(cls) -> None:
        config, reason = load_test_config()
        if config is None:
            raise unittest.SkipTest(f"configuration unavailable: {reason}")

        blocker = gcloud.preflight(config)
        if blocker is not None:
            raise unittest.SkipTest(blocker)

        if not bucket.bucket_exists(config):
            raise unittest.SkipTest(
                f"gs://{config.bucket_name} does not exist; run `python src/create_bucket.py`"
            )

        cls.config = config
