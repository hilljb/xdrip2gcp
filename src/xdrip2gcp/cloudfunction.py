"""Idempotent deployment of the Nightscout Cloud Function.

`gcloud functions deploy` already converges to the requested state, but it
rebuilds a container image every time, which takes minutes. So the deployed
function carries a label holding a hash of everything that affects it: its
source files, its deploy settings, and a fingerprint of the stored credential.
When that hash is unchanged there is nothing to do, and the deploy is skipped.

The credential fingerprint matters because secret environment variables are
resolved when an instance starts. Rotating the password therefore requires a
redeploy for the change to take effect, and folding the digest into the hash
makes that happen automatically.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import secretmanager
from .actions import ActionResult
from .config import Config
from .function_source import source_digest
from .gcloud import GcloudError, run

DEPLOY_TIMEOUT_SECONDS = 900
SOURCE_HASH_LABEL = "source-hash"


def function_env(config: Config) -> dict[str, str]:
    """Environment the function reads at runtime."""
    return {
        "XDRIP2GCP_BUCKET": config.bucket_name,
        "XDRIP2GCP_OBJECT_PREFIX": config.data_prefix,
        "XDRIP2GCP_AUTH_HEADER": config.auth.header_name,
        "XDRIP2GCP_MAX_REQUEST_BYTES": str(config.auth.max_request_bytes),
    }


def describe(config: Config) -> dict[str, Any] | None:
    """Return the deployed function's metadata, or None if it is absent."""
    result = run(
        config,
        [
            "functions",
            "describe",
            config.function.name,
            f"--region={config.function_region}",
            "--format=json",
        ],
        check=False,
    )
    if result.ok:
        return json.loads(result.stdout)
    if "not found" in result.stderr.lower() or "404" in result.stderr or "NOT_FOUND" in result.stderr:
        return None
    raise GcloudError(["functions", "describe"], result.returncode, result.stdout, result.stderr)


def function_url(config: Config, metadata: dict[str, Any] | None = None) -> str | None:
    """The HTTPS URL of the deployed function."""
    metadata = metadata if metadata is not None else describe(config)
    if metadata is None:
        return None
    service_config = metadata.get("serviceConfig") or {}
    return service_config.get("uri") or metadata.get("url")


def deployment_hash(config: Config, credential_fingerprint: str) -> str:
    """Hash everything that would change the deployed function."""
    function = config.function
    material = {
        "source": source_digest(function.source_path),
        "runtime": function.runtime,
        "entry_point": function.entry_point,
        "memory": function.memory,
        "timeout": function.timeout_seconds,
        "max_instances": function.max_instances,
        "allow_unauthenticated": function.allow_unauthenticated,
        "region": config.function_region,
        "service_account": config.runtime_service_account,
        "env": function_env(config),
        "secret": secretmanager.secret_version_reference(config),
        "credential": credential_fingerprint,
    }
    encoded = json.dumps(material, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def credential_fingerprint(config: Config) -> str:
    """A stable, non-secret marker for the currently stored digest."""
    payload = secretmanager.latest_payload(config)
    if payload is None:
        return "absent"
    marker = f"{payload.get('salt', '')}:{payload.get('digest', '')}"
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()[:16]


def deployed_hash(metadata: dict[str, Any]) -> str | None:
    return (metadata.get("labels") or {}).get(SOURCE_HASH_LABEL)


def deploy(config: Config, *, force: bool = False) -> ActionResult:
    """Deploy the function unless an identical deployment is already live."""
    function = config.function
    if not (function.source_path / "main.py").exists():
        raise GcloudError(["deploy"], 1, "", f"no main.py in {function.source_path}")

    fingerprint = credential_fingerprint(config)
    if fingerprint == "absent":
        raise GcloudError(
            ["deploy"], 1, "", "no credential stored yet; the secret must be created first"
        )

    wanted = deployment_hash(config, fingerprint)
    metadata = describe(config)
    if metadata is not None and not force and deployed_hash(metadata) == wanted:
        return ActionResult(False, f"{function.name} is already deployed with this source and config")

    env_pairs = ",".join(f"{key}={value}" for key, value in sorted(function_env(config).items()))
    args = [
        "functions",
        "deploy",
        function.name,
        "--gen2",
        f"--region={config.function_region}",
        f"--runtime={function.runtime}",
        f"--source={function.source_path}",
        f"--entry-point={function.entry_point}",
        "--trigger-http",
        f"--service-account={config.runtime_service_account}",
        f"--build-service-account=projects/{config.project_id}/serviceAccounts/{config.build_service_account}",
        f"--set-env-vars={env_pairs}",
        f"--set-secrets=NIGHTSCOUT_SECRET={secretmanager.secret_version_reference(config)}",
        f"--memory={function.memory}",
        f"--timeout={function.timeout_seconds}s",
        f"--max-instances={function.max_instances}",
        f"--update-labels={SOURCE_HASH_LABEL}={wanted}",
    ]
    args.append("--allow-unauthenticated" if function.allow_unauthenticated else "--no-allow-unauthenticated")

    run(config, args, timeout=DEPLOY_TIMEOUT_SECONDS)
    verb = "redeployed" if metadata is not None else "deployed"
    return ActionResult(True, f"{verb} {function.name} to {config.function_region}")


def summary(config: Config) -> dict[str, Any]:
    """The deployed function's facts worth printing or asserting on."""
    metadata = describe(config)
    if metadata is None:
        return {"exists": False, "name": config.function.name}

    service_config = metadata.get("serviceConfig") or {}
    return {
        "exists": True,
        "name": config.function.name,
        "state": metadata.get("state"),
        "url": function_url(config, metadata),
        "region": (metadata.get("name") or "").split("/locations/")[-1].split("/")[0],
        "runtime": (metadata.get("buildConfig") or {}).get("runtime"),
        "service_account": service_config.get("serviceAccountEmail"),
        "max_instances": service_config.get("maxInstanceCount"),
        "timeout_seconds": service_config.get("timeoutSeconds"),
        "source_hash": deployed_hash(metadata),
        "environment": service_config.get("environmentVariables") or {},
    }
