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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import secretmanager
from .actions import ActionResult
from .config import Config, FunctionConfig
from .function_source import source_digest, staged
from .gcloud import GcloudError, run

DEPLOY_TIMEOUT_SECONDS = 900
SOURCE_HASH_LABEL = "source-hash"


def function_env(config: Config) -> dict[str, str]:
    """Environment the bucket-backed function reads at runtime."""
    return {
        "XDRIP2GCP_BUCKET": config.bucket_name,
        "XDRIP2GCP_OBJECT_PREFIX": config.data_prefix,
        "XDRIP2GCP_AUTH_HEADER": config.auth.header_name,
        "XDRIP2GCP_MAX_REQUEST_BYTES": str(config.auth.max_request_bytes),
    }


def bq_function_env(config: Config) -> dict[str, str]:
    """Environment the BigQuery-backed function reads at runtime."""
    bigquery = config.bigquery
    return {
        "XDRIP2GCP_BQ_PROJECT": config.project_id,
        "XDRIP2GCP_BQ_DATASET": bigquery.dataset,
        "XDRIP2GCP_BQ_ENTRIES_TABLE": bigquery.entries_table,
        "XDRIP2GCP_BQ_LATEST_TABLE": bigquery.latest_table,
        "XDRIP2GCP_BQ_LATEST_ROWS": str(bigquery.latest_rows),
        "XDRIP2GCP_BQ_TIMEZONE": bigquery.timezone,
        "XDRIP2GCP_BQ_LOCATION": config.bigquery_location,
        "XDRIP2GCP_AUTH_HEADER": config.auth.header_name,
        "XDRIP2GCP_MAX_REQUEST_BYTES": str(config.auth.max_request_bytes),
    }


def _resolve(config: Config, function: FunctionConfig | None) -> FunctionConfig:
    """Default to the Stage 3 function, so existing callers are unaffected."""
    if function is not None:
        return function
    if config.function is None:
        raise GcloudError(["deploy"], 1, "", "[function] missing from configuration")
    return config.function


def describe(config: Config, function: FunctionConfig | None = None) -> dict[str, Any] | None:
    """Return the deployed function's metadata, or None if it is absent."""
    target = _resolve(config, function)
    result = run(
        config,
        [
            "functions",
            "describe",
            target.name,
            f"--region={config.region_of(target)}",
            "--format=json",
        ],
        check=False,
    )
    if result.ok:
        return json.loads(result.stdout)
    if "not found" in result.stderr.lower() or "404" in result.stderr or "NOT_FOUND" in result.stderr:
        return None
    raise GcloudError(["functions", "describe"], result.returncode, result.stdout, result.stderr)


def function_url(
    config: Config,
    metadata: dict[str, Any] | None = None,
    function: FunctionConfig | None = None,
) -> str | None:
    """The HTTPS URL of the deployed function."""
    metadata = metadata if metadata is not None else describe(config, function)
    if metadata is None:
        return None
    service_config = metadata.get("serviceConfig") or {}
    return service_config.get("uri") or metadata.get("url")


def deployment_hash(
    config: Config,
    credential_fingerprint: str,
    function: FunctionConfig | None = None,
    env: dict[str, str] | None = None,
    service_account: str | None = None,
    shared_modules: Sequence[Path] = (),
) -> str:
    """Hash everything that would change the deployed function."""
    target = _resolve(config, function)
    material = {
        "source": source_digest(target.source_path, shared_modules),
        "runtime": target.runtime,
        "entry_point": target.entry_point,
        "memory": target.memory,
        "timeout": target.timeout_seconds,
        "max_instances": target.max_instances,
        "allow_unauthenticated": target.allow_unauthenticated,
        "region": config.region_of(target),
        "service_account": service_account or config.runtime_service_account,
        "env": env if env is not None else function_env(config),
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


def deploy(
    config: Config,
    *,
    force: bool = False,
    function: FunctionConfig | None = None,
    env: dict[str, str] | None = None,
    service_account: str | None = None,
    shared_modules: Sequence[Path] = (),
) -> ActionResult:
    """Deploy the function unless an identical deployment is already live."""
    target = _resolve(config, function)
    region = config.region_of(target)
    identity = service_account or config.runtime_service_account
    environment = env if env is not None else function_env(config)

    if not (target.source_path / "main.py").exists():
        raise GcloudError(["deploy"], 1, "", f"no main.py in {target.source_path}")

    fingerprint = credential_fingerprint(config)
    if fingerprint == "absent":
        raise GcloudError(
            ["deploy"], 1, "", "no credential stored yet; the secret must be created first"
        )

    wanted = deployment_hash(config, fingerprint, target, environment, identity, shared_modules)
    metadata = describe(config, target)

    # A deploy that failed part-way still records its label, so the hash alone
    # would let the next run skip a function that is not serving. The state has
    # to agree that it is live.
    state = (metadata or {}).get("state")
    if metadata is not None and state == "ACTIVE" and not force and deployed_hash(metadata) == wanted:
        return ActionResult(False, f"{target.name} is already deployed with this source and config")

    env_pairs = ",".join(f"{key}={value}" for key, value in sorted(environment.items()))
    with staged(target.source_path, shared_modules) as source:
        args = [
            "functions",
            "deploy",
            target.name,
            "--gen2",
            f"--region={region}",
            f"--runtime={target.runtime}",
            f"--source={source}",
            f"--entry-point={target.entry_point}",
            "--trigger-http",
            f"--service-account={identity}",
            f"--build-service-account=projects/{config.project_id}/serviceAccounts/{config.build_service_account}",
            f"--set-env-vars={env_pairs}",
            f"--set-secrets=NIGHTSCOUT_SECRET={secretmanager.secret_version_reference(config)}",
            f"--memory={target.memory}",
            f"--timeout={target.timeout_seconds}s",
            f"--max-instances={target.max_instances}",
            f"--update-labels={SOURCE_HASH_LABEL}={wanted}",
        ]
        args.append("--allow-unauthenticated" if target.allow_unauthenticated else "--no-allow-unauthenticated")
        run(config, args, timeout=DEPLOY_TIMEOUT_SECONDS)

    if metadata is None:
        verb = "deployed"
    elif state != "ACTIVE":
        verb = f"redeployed (was {state})"
    else:
        verb = "redeployed"
    return ActionResult(True, f"{verb} {target.name} to {region}")


def summary(config: Config, function: FunctionConfig | None = None) -> dict[str, Any]:
    """The deployed function's facts worth printing or asserting on."""
    target = _resolve(config, function)
    metadata = describe(config, target)
    if metadata is None:
        return {"exists": False, "name": target.name}

    service_config = metadata.get("serviceConfig") or {}
    return {
        "exists": True,
        "name": target.name,
        "state": metadata.get("state"),
        "url": function_url(config, metadata, target),
        "region": (metadata.get("name") or "").split("/locations/")[-1].split("/")[0],
        "runtime": (metadata.get("buildConfig") or {}).get("runtime"),
        "service_account": service_config.get("serviceAccountEmail"),
        "max_instances": service_config.get("maxInstanceCount"),
        "timeout_seconds": service_config.get("timeoutSeconds"),
        "source_hash": deployed_hash(metadata),
        "environment": service_config.get("environmentVariables") or {},
    }
