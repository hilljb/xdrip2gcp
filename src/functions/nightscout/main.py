"""Cloud Function entry point for the Nightscout-compatible endpoint.

This module is only an adapter: it translates a Flask request into the
framework-free `Request` of `nightscout_core`, supplies a Cloud Storage writer,
and translates the result back. All behaviour worth testing lives in
`nightscout_core`, which has no cloud dependencies and is exercised offline.
"""

from __future__ import annotations

import os
import traceback
from typing import Any

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage

import nightscout_core as core

BUCKET_ENV = "XDRIP2GCP_BUCKET"
PREFIX_ENV = "XDRIP2GCP_OBJECT_PREFIX"
SECRET_ENV = "NIGHTSCOUT_SECRET"
HEADER_ENV = "XDRIP2GCP_AUTH_HEADER"
MAX_BYTES_ENV = "XDRIP2GCP_MAX_REQUEST_BYTES"

# Built once per instance and reused across requests: scrypt parameters and the
# storage client are both worth keeping warm.
_handler: core.Handler | None = None
_storage_client: storage.Client | None = None


class ConfigurationError(Exception):
    """Raised when the function's environment is incomplete."""


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"environment variable {name} is not set")
    return value


def _bucket() -> storage.Bucket:
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client()
    return _storage_client.bucket(_require_env(BUCKET_ENV))


def _write_object(path: str, data: bytes, content_type: str) -> bool:
    """Write an object unless it is already there.

    `if_generation_match=0` means "only if this object does not exist", so a
    duplicate upload is rejected by Cloud Storage rather than overwritten.
    Combined with content-addressed names, that makes retries free and lets the
    runtime identity hold create-only permission, with no delete rights at all.
    """
    blob = _bucket().blob(path)
    try:
        blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        return True
    except PreconditionFailed:
        return False


def _build_handler() -> core.Handler:
    payload = core.load_secret_payload(_require_env(SECRET_ENV))
    return core.Handler(
        secret_payload=payload,
        object_prefix=_require_env(PREFIX_ENV),
        writer=_write_object,
        bucket_name=_require_env(BUCKET_ENV),
        header_name=os.environ.get(HEADER_ENV, "api-secret"),
        max_request_bytes=int(os.environ.get(MAX_BYTES_ENV, "1048576")),
    )


def _get_handler() -> core.Handler:
    global _handler
    if _handler is None:
        _handler = _build_handler()
    return _handler


def nightscout(request: Any):
    """HTTP entry point; `--entry-point=nightscout` targets this function."""
    try:
        handler = _get_handler()
        core_request = core.Request.create(
            method=request.method,
            path=request.path,
            headers=dict(request.headers),
            query=request.args.to_dict(),
            body=request.get_data(cache=False) or b"",
        )
        response = handler.handle(core_request)
    except (ConfigurationError, core.SecretPayloadError) as error:
        # Misconfiguration, not a bad request: log the detail for the operator
        # and tell the caller only that the server is misconfigured.
        print(f"configuration error: {error}")
        response = core.error_response(500, "Server misconfigured")
    except Exception:  # noqa: BLE001 - never leak a traceback to the caller
        print(f"unhandled error:\n{traceback.format_exc()}")
        response = core.error_response(500, "Internal Server Error")

    return response.body, response.status, response.headers
