"""Idempotent Secret Manager operations for the Nightscout credential.

Only the scrypt digest of `sha1_hex(password)` ever reaches GCP. The plaintext
password stays in the git-ignored local config, which is the value typed into
xDrip; nothing stored here can be replayed against the endpoint.

The digest format is defined in the function's `nightscout_core`, which is
imported rather than duplicated so the deploy path and the runtime can never
disagree about how the credential is hashed.
"""

from __future__ import annotations

import json
import secrets
import tempfile
from pathlib import Path
from typing import Any

from .actions import ActionResult
from .config import Config
from .function_source import core_module
from .gcloud import GcloudError, run


def secret_exists(config: Config) -> bool:
    result = run(config, ["secrets", "describe", config.auth.secret_id], check=False)
    if result.ok:
        return True
    if "NOT_FOUND" in result.stderr or "not found" in result.stderr.lower() or "404" in result.stderr:
        return False
    raise GcloudError(["secrets", "describe"], result.returncode, result.stdout, result.stderr)


def latest_payload(config: Config) -> dict[str, Any] | None:
    """Return the current secret payload, or None if there is no version yet."""
    result = run(
        config,
        ["secrets", "versions", "access", "latest", f"--secret={config.auth.secret_id}"],
        check=False,
    )
    if not result.ok:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def ensure_secret(config: Config) -> ActionResult:
    """Create the secret container if it is missing."""
    if secret_exists(config):
        return ActionResult(False, f"secret {config.auth.secret_id} already exists")

    result = run(
        config,
        ["secrets", "create", config.auth.secret_id, "--replication-policy=automatic"],
        check=False,
    )
    if result.ok:
        return ActionResult(True, f"created secret {config.auth.secret_id}")
    if "ALREADY_EXISTS" in result.stderr or "409" in result.stderr:
        return ActionResult(False, f"secret {config.auth.secret_id} already exists")
    raise GcloudError(["secrets", "create"], result.returncode, result.stdout, result.stderr)


def _add_version(config: Config, payload: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(payload, handle, sort_keys=True)
        path = handle.name
    try:
        run(
            config,
            ["secrets", "versions", "add", config.auth.secret_id, f"--data-file={path}"],
        )
    finally:
        Path(path).unlink(missing_ok=True)


def ensure_password_version(config: Config) -> ActionResult:
    """Ensure the stored digest matches the local password.

    A new version is only added when the existing one does not verify against
    the configured password, or when the scrypt work factors have changed.
    Secret versions cost money and cannot be edited in place, so re-running the
    deploy must not mint a new one each time.
    """
    core = core_module()
    auth = config.auth
    existing = latest_payload(config)

    if existing is not None:
        try:
            matches = core.verify_credential(core.sha1_hex(auth.password), existing)
        except core.SecretPayloadError:
            matches = False

        parameters_current = all(
            [
                int(existing.get("n", -1)) == auth.scrypt_n,
                int(existing.get("r", -1)) == auth.scrypt_r,
                int(existing.get("p", -1)) == auth.scrypt_p,
                int(existing.get("dklen", -1)) == auth.scrypt_dklen,
            ]
        )
        if matches and parameters_current:
            return ActionResult(False, "stored digest already matches the local password")

        reason = "password changed" if not matches else "scrypt parameters changed"
        payload = core.build_secret_payload(
            auth.password,
            salt=secrets.token_bytes(auth.salt_bytes),
            n=auth.scrypt_n,
            r=auth.scrypt_r,
            p=auth.scrypt_p,
            dklen=auth.scrypt_dklen,
        )
        _add_version(config, payload)
        return ActionResult(True, f"added a new secret version ({reason})")

    payload = core.build_secret_payload(
        auth.password,
        salt=secrets.token_bytes(auth.salt_bytes),
        n=auth.scrypt_n,
        r=auth.scrypt_r,
        p=auth.scrypt_p,
        dklen=auth.scrypt_dklen,
    )
    _add_version(config, payload)
    return ActionResult(True, "stored the first secret version")


def ensure_accessor(config: Config, service_account: str | None = None) -> ActionResult:
    """Let one runtime identity read this one secret, and nothing else.

    Both functions share this credential, so each of their identities needs a
    binding here; the secret is the only thing they have in common.
    """
    member = f"serviceAccount:{service_account or config.runtime_service_account}"
    role = "roles/secretmanager.secretAccessor"

    policy = json.loads(
        run(config, ["secrets", "get-iam-policy", config.auth.secret_id, "--format=json"]).stdout
    )
    for binding in policy.get("bindings", []):
        if binding.get("role") == role and member in binding.get("members", []):
            return ActionResult(False, f"{member} already has {role} on {config.auth.secret_id}")

    run(
        config,
        [
            "secrets",
            "add-iam-policy-binding",
            config.auth.secret_id,
            f"--member={member}",
            f"--role={role}",
        ],
    )
    return ActionResult(True, f"granted {role} on {config.auth.secret_id} to {member}")


def secret_version_reference(config: Config) -> str:
    """The `--set-secrets` reference for the current secret."""
    return f"{config.secret_resource}/versions/latest"


def ensure_credential(config: Config, service_account: str | None = None) -> list[ActionResult]:
    """Bring the secret, its version and its IAM binding to the desired state."""
    return [
        ensure_secret(config),
        ensure_password_version(config),
        ensure_accessor(config, service_account),
    ]
