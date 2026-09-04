"""Nightscout-compatible request handling, with no cloud dependencies.

This module is deliberately stdlib-only and side-effect free: storage is an
injected callable and the clock is injectable too. That lets the whole request
path be exercised offline against a fake writer, while the deployed function
supplies a real Cloud Storage writer. It is also the single source of truth for
the credential hash format, imported both by the function at runtime and by the
local deploy script that populates Secret Manager.

Authentication follows Nightscout's wire protocol, which matters because xDrip
implements it: the client sends `sha1_hex(password)` in an `api-secret` header,
never the password itself. This module hashes that received digest a second
time with scrypt and compares it to a salted digest held in Secret Manager, so
GCP stores nothing that can be replayed against the endpoint.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import unquote

SERVER_NAME = "xdrip2gcp"
API_VERSION = "1"
PAYLOAD_VERSION = 1
NDJSON_CONTENT_TYPE = "application/x-ndjson"

# Nightscout's api-secret header carries a 40-character SHA-1 hex digest.
SHA1_HEX_PATTERN = re.compile(r"\A[0-9a-fA-F]{40}\Z")

# Collections xDrip and other Nightscout uploaders POST to.
WRITABLE_COLLECTIONS = ("entries", "treatments", "devicestatus")

# Path suffixes handled, relative to the Nightscout API root.
STATUS_ENDPOINTS = ("status", "status.json")
AUTH_CHECK_ENDPOINTS = ("experiments/test",)

API_ROOT = "/api/v1/"

SUPPORTED_ENDPOINTS = (
    "GET /api/v1/status",
    "GET /api/v1/experiments/test",
    "POST /api/v1/entries",
    "POST /api/v1/treatments",
    "POST /api/v1/devicestatus",
)


class PayloadError(Exception):
    """Raised when a request body cannot be accepted."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class SecretPayloadError(Exception):
    """Raised when the credential digest from Secret Manager is unusable."""


@dataclass(frozen=True)
class Request:
    """A normalized HTTP request, independent of any web framework."""

    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    @classmethod
    def create(
        cls,
        method: str,
        path: str,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> "Request":
        """Build a request with header names folded to lowercase."""
        folded = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        return cls(method=method.upper(), path=path or "/", headers=folded, query=dict(query or {}), body=body)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(frozen=True)
class Response:
    """A JSON HTTP response."""

    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "application/json")

    def json(self) -> Any:
        return json.loads(self.body)


def json_response(status: int, payload: Any, headers: Mapping[str, str] | None = None) -> Response:
    body = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    return Response(status=status, body=body, headers=merged)


def error_response(status: int, message: str, **extra: Any) -> Response:
    payload = {"status": status, "message": message}
    payload.update(extra)
    return json_response(status, payload)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def sha1_hex(password: str) -> str:
    """The value a Nightscout client puts in the `api-secret` header.

    SHA-1 is fixed by the Nightscout protocol, not chosen here; the strength
    of this scheme comes from the password's entropy and the scrypt digest
    applied on top.
    """
    return hashlib.sha1(password.encode("utf-8")).hexdigest()


def normalize_credential(raw: str) -> str:
    """Reduce a supplied credential to the canonical SHA-1 hex form.

    A 40-character hex string is taken to be a digest already, which is what
    xDrip and every other Nightscout client sends. Anything else is treated as
    a plaintext password and hashed, so `curl -H 'api-secret: <password>'`
    works for hand testing.
    """
    candidate = raw.strip()
    if SHA1_HEX_PATTERN.match(candidate):
        return candidate.lower()
    return sha1_hex(candidate)


def _credential_from_basic_auth(header_value: str) -> str | None:
    """Pull a credential out of a Basic auth header.

    xDrip is configured with `https://password@host/api/v1/`, and some HTTP
    stacks turn that userinfo into Basic auth. Either half may hold the
    password depending on how the URL was written.
    """
    if not header_value.lower().startswith("basic "):
        return None
    encoded = header_value[6:].strip()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None

    username, _, password = decoded.partition(":")
    return password or username or None


def extract_credential(request: Request, header_name: str = "api-secret") -> str | None:
    """Find the caller's credential in any of the places clients put it."""
    header_value = request.header(header_name)
    if header_value and header_value.strip():
        return header_value.strip()

    authorization = request.header("authorization")
    if authorization:
        from_basic = _credential_from_basic_auth(authorization)
        if from_basic:
            return from_basic

    for parameter in ("secret", "token"):
        value = request.query.get(parameter)
        if value and value.strip():
            return unquote(value.strip())

    return None


def _scrypt_maxmem(n: int, r: int, p: int) -> int:
    """Memory ceiling scrypt needs, computed the way OpenSSL checks it."""
    return 128 * r * (n + p + 2) + 1024


def scrypt_digest(credential: str, salt_hex: str, n: int, r: int, p: int, dklen: int) -> str:
    """Hash an already-SHA-1'd credential with salted scrypt."""
    return hashlib.scrypt(
        credential.encode("utf-8"),
        salt=bytes.fromhex(salt_hex),
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=_scrypt_maxmem(n, r, p),
    ).hex()


def build_secret_payload(
    password: str,
    *,
    salt: bytes,
    n: int = 16384,
    r: int = 8,
    p: int = 1,
    dklen: int = 32,
) -> dict[str, Any]:
    """Build the self-describing digest document stored in Secret Manager.

    Storing the parameters alongside the digest means the work factors can be
    raised later without the function needing to guess which scheme an older
    secret version used.
    """
    salt_hex = salt.hex()
    credential = sha1_hex(password)
    return {
        "version": PAYLOAD_VERSION,
        "algorithm": "scrypt",
        "credential_hash": "sha1",
        "n": n,
        "r": r,
        "p": p,
        "dklen": dklen,
        "salt": salt_hex,
        "digest": scrypt_digest(credential, salt_hex, n, r, p, dklen),
    }


def load_secret_payload(raw: str) -> dict[str, Any]:
    """Parse and validate the Secret Manager payload."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SecretPayloadError(f"secret is not valid JSON: {error}") from error

    if not isinstance(payload, dict):
        raise SecretPayloadError("secret must be a JSON object")
    if payload.get("algorithm") != "scrypt":
        raise SecretPayloadError(f"unsupported algorithm: {payload.get('algorithm')!r}")

    for key in ("salt", "digest", "n", "r", "p", "dklen"):
        if key not in payload:
            raise SecretPayloadError(f"secret is missing {key!r}")
    return payload


def verify_credential(credential: str, payload: Mapping[str, Any]) -> bool:
    """Constant-time check of a supplied credential against the stored digest."""
    try:
        computed = scrypt_digest(
            normalize_credential(credential),
            str(payload["salt"]),
            int(payload["n"]),
            int(payload["r"]),
            int(payload["p"]),
            int(payload["dklen"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SecretPayloadError(f"stored digest is unusable: {error}") from error

    return hmac.compare_digest(computed, str(payload["digest"]))


# --------------------------------------------------------------------------
# Payloads and object naming
# --------------------------------------------------------------------------


def parse_documents(body: bytes, max_bytes: int) -> list[dict[str, Any]]:
    """Validate a request body into a list of Nightscout documents."""
    if len(body) > max_bytes:
        raise PayloadError(f"request body exceeds {max_bytes} bytes", status=413)
    if not body.strip():
        raise PayloadError("request body is empty")

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise PayloadError(f"request body is not valid JSON: {error.msg}")

    documents = parsed if isinstance(parsed, list) else [parsed]
    if not documents:
        raise PayloadError("request body contains no documents")
    if not all(isinstance(document, dict) for document in documents):
        raise PayloadError("every document must be a JSON object")
    return documents


def to_ndjson(documents: list[dict[str, Any]]) -> bytes:
    """Serialize documents as newline-delimited JSON.

    NDJSON is what BigQuery ingests natively, which is where this data is
    ultimately headed. Keys are sorted so the bytes are canonical: a client
    that resends the same readings with keys in a different order still
    produces an identical object.
    """
    lines = [json.dumps(document, sort_keys=True, separators=(",", ":")) for document in documents]
    return ("\n".join(lines) + "\n").encode("utf-8")


def content_hash(data: bytes, length: int = 16) -> str:
    return hashlib.sha256(data).hexdigest()[:length]


def object_path(prefix: str, collection: str, when: datetime, data: bytes) -> str:
    """Build the object name for a batch of documents.

    Hive-style `collection=`/`dt=` partitioning is directly readable by
    BigQuery external tables. Naming the object after a hash of its own
    contents is what makes the write idempotent: xDrip retries a queued upload
    after an outage, and the retry resolves to the object already stored rather
    than to a duplicate.
    """
    day = when.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return f"{prefix.strip('/')}/collection={collection}/dt={day}/{content_hash(data)}.ndjson"


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def api_suffix(path: str) -> str | None:
    """Return the part of a request path after the Nightscout API root.

    Matching on the `/api/v1/` marker anywhere in the path means the same code
    serves both a Cloud Run style URL, where the path is exactly
    `/api/v1/entries`, and a cloudfunctions.net URL, where the function name
    precedes it.
    """
    marker = path.find(API_ROOT)
    if marker == -1:
        return None
    return path[marker + len(API_ROOT) :].strip("/")


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------

# Returns True when the object was newly written, False when an identical
# object was already stored.
StorageWriter = Callable[[str, bytes, str], bool]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Handler:
    """Serves Nightscout-style requests, writing documents through `writer`."""

    secret_payload: Mapping[str, Any]
    object_prefix: str
    writer: StorageWriter
    bucket_name: str = ""
    header_name: str = "api-secret"
    max_request_bytes: int = 1048576
    now: Callable[[], datetime] = _utcnow

    def handle(self, request: Request) -> Response:
        suffix = api_suffix(request.path)
        if suffix is None:
            return error_response(
                404, "Not Found", hint=f"endpoints live under {API_ROOT}", endpoints=list(SUPPORTED_ENDPOINTS)
            )

        # Nightscout serves status without authentication, and clients use it
        # as a reachability check before uploading.
        if suffix in STATUS_ENDPOINTS:
            if request.method not in ("GET", "HEAD"):
                return error_response(405, "Method Not Allowed", allowed=["GET"])
            return self._status_response()

        if suffix in AUTH_CHECK_ENDPOINTS:
            failure = self._authenticate(request)
            if failure is not None:
                return failure
            return json_response(200, {"status": "ok", "message": "authorized"})

        collection = suffix.removesuffix(".json")
        if collection not in WRITABLE_COLLECTIONS:
            return error_response(404, "Not Found", endpoints=list(SUPPORTED_ENDPOINTS))

        if request.method != "POST":
            return error_response(405, "Method Not Allowed", allowed=["POST"])

        failure = self._authenticate(request)
        if failure is not None:
            return failure

        return self._store(collection, request)

    def _status_response(self) -> Response:
        return json_response(
            200,
            {
                "status": "ok",
                "apiEnabled": True,
                "careportalEnabled": False,
                "name": SERVER_NAME,
                "version": f"{SERVER_NAME}-stage3",
                "apiVersion": API_VERSION,
                "serverTime": self.now().isoformat().replace("+00:00", "Z"),
                "settings": {"units": "mg/dl"},
            },
        )

    def _authenticate(self, request: Request) -> Response | None:
        """Return an error response when the caller is not authorized."""
        credential = extract_credential(request, self.header_name)
        if credential is None:
            return error_response(
                401,
                "Unauthorized",
                hint=f"send sha1_hex(password) in the {self.header_name!r} header",
            )
        if not verify_credential(credential, self.secret_payload):
            return error_response(401, "Unauthorized")
        return None

    def _store(self, collection: str, request: Request) -> Response:
        try:
            documents = parse_documents(request.body, self.max_request_bytes)
        except PayloadError as error:
            return error_response(error.status, error.message)

        data = to_ndjson(documents)
        path = object_path(self.object_prefix, collection, self.now(), data)
        created = self.writer(path, data, NDJSON_CONTENT_TYPE)

        # The body echoes the stored documents the way Nightscout does, so a
        # real client sees nothing unexpected; our own metadata rides along in
        # headers where tests can assert on it.
        return json_response(
            200,
            documents,
            headers={
                "x-xdrip2gcp-object": path,
                "x-xdrip2gcp-bucket": self.bucket_name,
                "x-xdrip2gcp-stored": "new" if created else "duplicate",
                "x-xdrip2gcp-documents": str(len(documents)),
            },
        )
