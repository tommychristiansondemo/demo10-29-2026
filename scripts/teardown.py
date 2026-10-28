#!/usr/bin/env python3
"""PharmaPulse teardown script (Requirements 18.4, 18.5).

Removes every deployed PharmaPulse CloudFormation stack EXCEPT the Route 53
public hosted zone ``awsteach.com``. The hosted zone is a *pre-existing*
resource that the edge certificate/hosting stacks look up (they never create
it), so teardown must never delete it (Requirement 18.4).

Stacks are deleted in **reverse dependency order** (the inverse of the
``app.py`` deployment order) so that a stack is torn down only after the
stacks that depend on it are already gone:

    deploy:  foundation -> auth -> data -> mcp_* -> gateway -> agent
             -> backend -> edge_cert -> edge_hosting -> observability
    teardown: observability/billing -> edge_hosting -> edge_cert -> backend
             -> agent -> gateway -> mcp_* -> data -> auth -> foundation

If any non-hosted-zone resource fails to delete, teardown ABORTS immediately
(it does not proceed to the remaining stacks), reports which stack/resource
failed and why, and returns a non-zero exit code (Requirement 18.5).

The CloudFormation client is injectable so this module can be exercised with a
fake client in unit/operational tests (task 20.5) without touching AWS. The
real boto3 client is created lazily only when no client is injected.

Usage::

    python -m scripts.teardown                 # tear everything down
    python -m scripts.teardown --region us-east-2
    python -m scripts.teardown --dry-run       # print the plan, delete nothing
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Sequence

from stacks.config import EDGE_REGION, HOSTED_ZONE_NAME, PRIMARY_REGION

# ---------------------------------------------------------------------------
# Stack inventory
# ---------------------------------------------------------------------------
# Deployment order per app.py. Teardown walks this in REVERSE so dependents
# are removed before their dependencies.
DEPLOY_ORDER: Sequence[str] = (
    "PharmaPulse-Foundation",
    "PharmaPulse-Auth",
    "PharmaPulse-Data",
    "PharmaPulse-McpFda",
    "PharmaPulse-McpInventory",
    "PharmaPulse-Gateway",
    "PharmaPulse-Agent",
    "PharmaPulse-Backend",
    "PharmaPulse-EdgeCert",
    "PharmaPulse-EdgeHosting",
    "PharmaPulse-Observability",
    "PharmaPulse-BillingAlarm",
)

# Stacks that live in the Edge_Region (us-east-1); everything else is primary.
# These are the only stacks CloudFormation must be addressed in us-east-1 for
# (ACM cert + CloudFront/WAF/Route53 alias hosting + the billing alarm).
EDGE_STACKS: frozenset[str] = frozenset(
    {
        "PharmaPulse-EdgeCert",
        "PharmaPulse-EdgeHosting",
        "PharmaPulse-BillingAlarm",
    }
)

# The pre-existing Route 53 public hosted zone that must NEVER be deleted
# (Requirement 18.4). It is a *logical* resource name here — teardown operates
# on CloudFormation stacks, and none of the stacks above own the hosted zone
# (it is looked up, not created), but we guard explicitly regardless.
PROTECTED_HOSTED_ZONE = HOSTED_ZONE_NAME


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
class CloudFormationClient(Protocol):
    """The subset of the boto3 CloudFormation client teardown uses.

    Kept minimal so tests can supply a tiny fake implementing only these
    members.
    """

    def delete_stack(self, *, StackName: str) -> dict: ...

    def get_waiter(self, waiter_name: str): ...

    def describe_stacks(self, *, StackName: str) -> dict: ...


@dataclass
class StackResult:
    """Outcome of attempting to delete a single stack."""

    stack_name: str
    region: str
    status: str  # "deleted" | "skipped" | "absent" | "failed"
    reason: str = ""


@dataclass
class TeardownReport:
    """Aggregate result of a teardown run."""

    results: List[StackResult] = field(default_factory=list)
    aborted: bool = False
    failure: Optional[StackResult] = None

    @property
    def ok(self) -> bool:
        return not self.aborted and self.failure is None


class TeardownError(RuntimeError):
    """Raised internally when a non-hosted-zone stack fails to delete."""

    def __init__(self, result: StackResult):
        super().__init__(f"{result.stack_name}: {result.reason}")
        self.result = result


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without any AWS client)
# ---------------------------------------------------------------------------
def teardown_order(deploy_order: Sequence[str] = DEPLOY_ORDER) -> List[str]:
    """Return the stacks in reverse-dependency (teardown) order."""
    return list(reversed(list(deploy_order)))


def region_for_stack(
    stack_name: str,
    primary_region: str = PRIMARY_REGION,
    edge_region: str = EDGE_REGION,
) -> str:
    """Return the AWS region a given stack is deployed in."""
    return edge_region if stack_name in EDGE_STACKS else primary_region


def is_protected_hosted_zone(name: str) -> bool:
    """True if ``name`` refers to the protected pre-existing hosted zone.

    Guards against ever issuing a delete for the Route 53 hosted zone
    ``awsteach.com`` (Requirement 18.4), matching either the bare zone name or
    a trailing-dot FQDN form.
    """
    if not name:
        return False
    normalized = name.strip().rstrip(".").lower()
    return normalized == PROTECTED_HOSTED_ZONE.strip().rstrip(".").lower()


def _stack_exists(client: CloudFormationClient, stack_name: str) -> bool:
    """Return True if the stack currently exists (best-effort).

    A missing stack is treated as already-deleted rather than a failure so
    teardown is idempotent. Any other describe error is surfaced to the caller.
    """
    try:
        resp = client.describe_stacks(StackName=stack_name)
    except Exception as exc:  # noqa: BLE001 - map "does not exist" to absent
        if "does not exist" in str(exc):
            return False
        raise
    stacks = resp.get("Stacks") or []
    if not stacks:
        return False
    # A stack already in DELETE_COMPLETE is effectively gone.
    return stacks[0].get("StackStatus") != "DELETE_COMPLETE"


# ---------------------------------------------------------------------------
# Deletion of a single stack
# ---------------------------------------------------------------------------
def delete_stack(
    client: CloudFormationClient,
    stack_name: str,
    *,
    region: str,
    log: Callable[[str], None] = print,
) -> StackResult:
    """Delete a single stack and confirm deletion, returning a StackResult.

    Raises :class:`TeardownError` if the stack fails to delete so the caller
    can abort the remaining teardown (Requirement 18.5).
    """
    # Defense-in-depth: never issue a delete against the protected hosted zone
    # even if a stack name somehow resolved to it (Requirement 18.4).
    if is_protected_hosted_zone(stack_name):
        log(f"[skip] {stack_name}: protected hosted zone, not deleting")
        return StackResult(stack_name, region, "skipped", "protected hosted zone")

    if not _stack_exists(client, stack_name):
        log(f"[absent] {stack_name}: already gone, nothing to delete")
        return StackResult(stack_name, region, "absent", "stack does not exist")

    log(f"[delete] {stack_name} (region={region}) ...")
    try:
        client.delete_stack(StackName=stack_name)
        waiter = client.get_waiter("stack_delete_complete")
        waiter.wait(StackName=stack_name)
    except Exception as exc:  # noqa: BLE001 - any failure aborts teardown
        reason = _explain_failure(client, stack_name, exc)
        result = StackResult(stack_name, region, "failed", reason)
        raise TeardownError(result) from exc

    log(f"[done] {stack_name}: deleted")
    return StackResult(stack_name, region, "deleted")


def _explain_failure(
    client: CloudFormationClient, stack_name: str, exc: Exception
) -> str:
    """Build a human-readable reason for a stack delete failure.

    Attempts to enrich the raw error with the stack's failure status / reason
    from CloudFormation when available, so the report says *which resource*
    failed and *why* (Requirement 18.5). Best-effort: falls back to the raw
    exception text if the stack can't be described.
    """
    detail = str(exc).strip()
    try:
        resp = client.describe_stacks(StackName=stack_name)
        stacks = resp.get("Stacks") or []
        if stacks:
            status = stacks[0].get("StackStatus", "")
            status_reason = stacks[0].get("StackStatusReason", "")
            parts = [p for p in (status, status_reason) if p]
            if parts:
                detail = f"{detail} (stack status: {'; '.join(parts)})"
    except Exception:  # noqa: BLE001 - enrichment is best-effort only
        pass
    return detail or f"{type(exc).__name__}"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _default_client_factory(region: str) -> CloudFormationClient:
    """Lazily build a real boto3 CloudFormation client for ``region``."""
    import boto3  # imported lazily so tests need not have AWS configured

    return boto3.client("cloudformation", region_name=region)


def run_teardown(
    *,
    client_factory: Optional[Callable[[str], CloudFormationClient]] = None,
    deploy_order: Sequence[str] = DEPLOY_ORDER,
    primary_region: str = PRIMARY_REGION,
    edge_region: str = EDGE_REGION,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> TeardownReport:
    """Tear down all PharmaPulse stacks in reverse dependency order.

    ``client_factory`` maps a region to a CloudFormation client; it defaults to
    a lazily-created boto3 client. Tests inject a factory returning a fake
    client (optionally one that simulates a delete failure).

    On the first non-hosted-zone stack failure, teardown ABORTS: it stops
    deleting further stacks, records the failure, and the returned report has
    ``ok is False`` (Requirement 18.5).
    """
    factory = client_factory or _default_client_factory
    report = TeardownReport()
    order = teardown_order(deploy_order)

    log("PharmaPulse teardown plan (reverse dependency order):")
    for name in order:
        region = region_for_stack(name, primary_region, edge_region)
        log(f"  - {name} ({region})")
    log(
        f"Protected (never deleted): Route 53 hosted zone "
        f"'{PROTECTED_HOSTED_ZONE}'"
    )

    if dry_run:
        log("[dry-run] no resources will be deleted")
        for name in order:
            region = region_for_stack(name, primary_region, edge_region)
            report.results.append(
                StackResult(name, region, "skipped", "dry-run")
            )
        return report

    # Cache one client per region so the same injected fake is reused.
    clients: dict[str, CloudFormationClient] = {}

    for name in order:
        region = region_for_stack(name, primary_region, edge_region)
        client = clients.get(region)
        if client is None:
            client = factory(region)
            clients[region] = client
        try:
            result = delete_stack(client, name, region=region, log=log)
        except TeardownError as err:
            report.results.append(err.result)
            report.failure = err.result
            report.aborted = True
            log(
                f"[abort] Teardown aborted: failed to delete "
                f"'{err.result.stack_name}' in {err.result.region}: "
                f"{err.result.reason}"
            )
            log(
                "[abort] Remaining stacks were NOT deleted. Resolve the "
                "failure above and re-run teardown."
            )
            return report
        report.results.append(result)

    log("[complete] All PharmaPulse stacks removed; hosted zone preserved.")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teardown",
        description=(
            "Remove all deployed PharmaPulse CloudFormation stacks except the "
            "pre-existing Route 53 hosted zone. Aborts and reports if any "
            "non-hosted-zone resource fails to delete."
        ),
    )
    parser.add_argument(
        "--region",
        default=PRIMARY_REGION,
        help=(
            "Primary AWS region for non-edge stacks "
            f"(default: {PRIMARY_REGION}). Edge stacks always use "
            f"{EDGE_REGION}."
        ),
    )
    parser.add_argument(
        "--edge-region",
        default=EDGE_REGION,
        help=f"AWS region for edge stacks (default: {EDGE_REGION}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the teardown plan without deleting anything.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint. Returns a non-zero exit code on any failure."""
    args = _build_parser().parse_args(argv)
    report = run_teardown(
        primary_region=args.region,
        edge_region=args.edge_region,
        dry_run=args.dry_run,
    )
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    sys.exit(main())
