"""Idempotent project provisioning: APIs, service accounts and IAM bindings.

Everything here checks current state before acting, so a second run reports
no-ops. The function gets two dedicated identities instead of GCP's default
service accounts, which carry broad project-wide roles:

* a build identity, used only by Cloud Build while packaging the source, and
* a runtime identity, which may create objects in one bucket and read one
  secret, and has no other permission anywhere in the project.
"""

from __future__ import annotations

import json
import time

from .actions import ActionResult
from .config import Config
from .gcloud import GcloudError, run

# A freshly created service account is not immediately visible to the IAM
# policy APIs, which reject bindings for members they cannot resolve yet.
IAM_PROPAGATION_ATTEMPTS = 6
IAM_PROPAGATION_DELAY_SECONDS = 5


def enabled_services(config: Config) -> set[str]:
    result = run(
        config,
        ["services", "list", "--enabled", "--format=value(config.name)"],
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def enable_services(config: Config) -> list[ActionResult]:
    """Enable every API in `[gcp].services` that is not already on."""
    if not config.services:
        return [ActionResult(False, "no services configured")]

    already_on = enabled_services(config)
    missing = [name for name in config.services if name not in already_on]
    if not missing:
        return [ActionResult(False, f"all {len(config.services)} required APIs already enabled")]

    # Enabling in one call lets the API resolve interdependencies itself.
    run(config, ["services", "enable", *missing])
    return [ActionResult(True, f"enabled {len(missing)} API(s): {', '.join(missing)}")]


def service_account_exists(config: Config, email: str) -> bool:
    result = run(config, ["iam", "service-accounts", "describe", email], check=False)
    if result.ok:
        return True
    if "NOT_FOUND" in result.stderr or "404" in result.stderr or "not found" in result.stderr.lower():
        return False
    raise GcloudError(["iam", "service-accounts", "describe", email], result.returncode, result.stdout, result.stderr)


def ensure_service_account(config: Config, account_id: str, display_name: str) -> ActionResult:
    """Create a service account unless it already exists."""
    email = config.service_account_email(account_id)
    if service_account_exists(config, email):
        return ActionResult(False, f"service account {email} already exists")

    result = run(
        config,
        [
            "iam",
            "service-accounts",
            "create",
            account_id,
            f"--display-name={display_name}",
        ],
        check=False,
    )
    if result.ok:
        return ActionResult(True, f"created service account {email}")
    if "ALREADY_EXISTS" in result.stderr or "409" in result.stderr:
        return ActionResult(False, f"service account {email} already exists")
    raise GcloudError(["iam", "service-accounts", "create", account_id], result.returncode, result.stdout, result.stderr)


def _policy_has_binding(policy: dict, role: str, member: str) -> bool:
    for binding in policy.get("bindings", []):
        if binding.get("role") == role and member in binding.get("members", []):
            return True
    return False


def _add_binding_with_retry(config: Config, args: list[str], member: str, role: str) -> None:
    """Add an IAM binding, waiting out service account propagation delay."""
    last_error: GcloudError | None = None
    for attempt in range(IAM_PROPAGATION_ATTEMPTS):
        result = run(config, args, check=False)
        if result.ok:
            return
        last_error = GcloudError(args, result.returncode, result.stdout, result.stderr)
        transient = "does not exist" in result.stderr or "Service account" in result.stderr
        if not transient or attempt == IAM_PROPAGATION_ATTEMPTS - 1:
            break
        time.sleep(IAM_PROPAGATION_DELAY_SECONDS)
    assert last_error is not None
    raise last_error


def ensure_project_role(config: Config, member: str, role: str) -> ActionResult:
    """Grant a project-level role unless the member already holds it."""
    policy = json.loads(
        run(config, ["projects", "get-iam-policy", config.project_id, "--format=json"]).stdout
    )
    if _policy_has_binding(policy, role, member):
        return ActionResult(False, f"{member} already has {role} on the project")

    _add_binding_with_retry(
        config,
        [
            "projects",
            "add-iam-policy-binding",
            config.project_id,
            f"--member={member}",
            f"--role={role}",
            "--condition=None",
        ],
        member,
        role,
    )
    return ActionResult(True, f"granted {role} on the project to {member}")


def ensure_bucket_role(config: Config, member: str, role: str) -> ActionResult:
    """Grant a role on the bucket alone, rather than project-wide."""
    policy = json.loads(
        run(
            config,
            ["storage", "buckets", "get-iam-policy", config.bucket_uri, "--format=json"],
        ).stdout
    )
    if _policy_has_binding(policy, role, member):
        return ActionResult(False, f"{member} already has {role} on {config.bucket_uri}")

    _add_binding_with_retry(
        config,
        [
            "storage",
            "buckets",
            "add-iam-policy-binding",
            config.bucket_uri,
            f"--member={member}",
            f"--role={role}",
        ],
        member,
        role,
    )
    return ActionResult(True, f"granted {role} on {config.bucket_uri} to {member}")


def ensure_service_accounts(config: Config) -> list[ActionResult]:
    """Create both identities and grant them their (only) roles."""
    if config.service_accounts is None:
        raise GcloudError(["provision"], 1, "", "[service_accounts] missing from configuration")

    accounts = config.service_accounts
    results = [
        ensure_service_account(config, accounts.runtime_id, "xdrip2gcp function runtime"),
        ensure_service_account(config, accounts.build_id, "xdrip2gcp function build"),
    ]

    runtime_member = f"serviceAccount:{config.runtime_service_account}"
    build_member = f"serviceAccount:{config.build_service_account}"

    results.append(ensure_bucket_role(config, runtime_member, accounts.runtime_role_bucket))
    results.append(ensure_project_role(config, build_member, accounts.build_role_project))
    return results
