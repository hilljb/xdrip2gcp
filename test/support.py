"""Shared helpers for the test suite."""

from __future__ import annotations

import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping

from xdrip2gcp import bucket, cloudfunction, gcloud
from xdrip2gcp.config import Config, ConfigError, load_config

# Generous enough to absorb a Cloud Run cold start on the first request.
HTTP_TIMEOUT_SECONDS = 60


def load_test_config() -> tuple[Config | None, str | None]:
    """Load config, returning either a config or the reason it is unavailable."""
    try:
        # Tests never invent a new bucket name; they use the one already
        # recorded by src/create_bucket.py.
        return load_config(allow_generation=False), None
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


@dataclass(frozen=True)
class HttpResult:
    """An HTTP response, whatever the status code."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None

    def json(self):
        import json

        return json.loads(self.body)


def http_request(
    url: str,
    method: str = "GET",
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> HttpResult:
    """Call the deployed endpoint, treating error statuses as results.

    Uses `urllib` from the standard library rather than `requests`, keeping the
    test suite dependency-free.
    """
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return HttpResult(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as error:
        # An HTTPError is itself a response object and holds an open socket.
        with error:
            return HttpResult(error.code, dict(error.headers), error.read())


class LiveFunctionTestCase(LiveGcpTestCase):
    """Base class for tests against the deployed Cloud Function.

    Skips when the function has not been deployed or no password is recorded,
    so the suite still runs on a clone that has not deployed anything.
    """

    base_url: str
    password: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()

        if not cls.config.auth.password:
            raise unittest.SkipTest(
                "no Nightscout password recorded; run `python src/deploy_function.py`"
            )

        url = cloudfunction.function_url(cls.config)
        if url is None:
            raise unittest.SkipTest(
                f"{cls.config.function.name} is not deployed; run `python src/deploy_function.py`"
            )

        cls.base_url = url.rstrip("/")
        cls.password = cls.config.auth.password

    @classmethod
    def api_url(cls, endpoint: str, query: str = "") -> str:
        url = f"{cls.base_url}/api/v1/{endpoint.lstrip('/')}"
        return f"{url}?{query}" if query else url
