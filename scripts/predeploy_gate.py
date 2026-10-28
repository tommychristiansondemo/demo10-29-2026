"""Pre-deployment verification gate for PharmaPulse (Requirement 19).

This module implements an *approval and prerequisite gate* that MUST pass
before any PharmaPulse resources are deployed. It never deploys anything
itself — it only decides whether a deploy is allowed to proceed and returns a
``DEPLOY-OK`` or ``NO-DEPLOY`` status accordingly.

The gate enforces the four pre-deployment checks from Requirement 19:

* **19.1** — Consult the current Amazon Bedrock AgentCore documentation for
  Runtime, Gateway, Identity, Memory, Observability, and Policy. This is
  modelled as a documented checklist of the canonical doc URLs (taken from the
  design document) plus an explicit *confirmation* that the instructor has
  consulted them. No network fetch happens at import or implicitly — the
  instructor confirms via a flag so the gate stays deterministic and offline.
* **19.2** — Verify Bedrock model access for the target Claude Sonnet model by
  listing foundation models (via an injectable boto3 ``bedrock`` client) and
  confirming the model is present and not gated.
* **19.3** — Confirm the Route 53 public hosted zone ``awsteach.com`` exists
  (via an injectable boto3 ``route53`` client).
* **19.4 / 19.5** — Require explicit architecture-plan approval. The gate
  returns ``NO-DEPLOY`` unless *all* prerequisite checks pass **and** approval
  is present; only then does it return ``DEPLOY-OK``.

Design for testability
-----------------------
All AWS clients are *injectable*. Pure decision functions (``decide_gate``,
``aggregate_checks``) take plain data so unit tests (task 20.5) can assert the
gating logic — notably that the gate returns ``NO-DEPLOY`` when approval or
any prerequisite is absent — without touching AWS. boto3 is imported lazily so
importing this module never requires AWS credentials or network access.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional, Sequence

# Reuse the single source of truth for the hosted zone and target model so the
# gate can never drift from what the stacks actually deploy.
from stacks.config import EDGE_REGION, HOSTED_ZONE_NAME, PRIMARY_REGION

# The latest Claude Sonnet model/inference-profile id the agent runs on
# (Requirement 7.1). Mirrors agent/config.py and stacks/agent_stack.py so the
# access check targets the model that will actually be invoked.
TARGET_CLAUDE_SONNET_MODEL = "us.anthropic.claude-sonnet-4-20250514-v1:0"

# Environment variable an instructor can set instead of passing --approve.
ENV_APPROVE = "APPROVE_ARCHITECTURE"
# Environment variable confirming the AgentCore docs were consulted (19.1).
ENV_DOCS_CONSULTED = "AGENTCORE_DOCS_CONSULTED"


# ---------------------------------------------------------------------------
# AgentCore documentation checklist (Requirement 19.1)
# ---------------------------------------------------------------------------
# The canonical AgentCore documentation that must be consulted before deploy,
# covering Runtime, Gateway, Identity, Memory, Observability, and Policy. URLs
# are sourced from the design document's research summary. This is a *static*
# checklist — the gate does not fetch these at runtime; it records them and
# requires an explicit confirmation flag that they were consulted.
AGENTCORE_DOC_CHECKLIST: tuple[tuple[str, str], ...] = (
    (
        "Gateway",
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
        "gateway-target-MCPservers.html",
    ),
    (
        "Gateway (outbound auth)",
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
        "gateway-outbound-auth.html",
    ),
    (
        "Policy",
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
        "policy-permissions.html",
    ),
    (
        "Policy (scope)",
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
        "policy-scope.html",
    ),
    (
        "Memory",
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
        "memory-organization.html",
    ),
    (
        "Identity / Memory (Strands SDK)",
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
        "strands-sdk-memory.html",
    ),
    (
        "Observability",
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
        "observability-configure.html",
    ),
    (
        "Runtime",
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
        "runtime.html",
    ),
    (
        "Identity",
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
        "identity.html",
    ),
)


class GateStatus(str, Enum):
    """Terminal gate decision."""

    DEPLOY_OK = "DEPLOY-OK"
    NO_DEPLOY = "NO-DEPLOY"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single prerequisite check.

    ``passed`` is the only field the aggregate decision depends on; ``name``
    and ``detail`` exist for human-readable reporting.
    """

    name: str
    passed: bool
    detail: str = ""

    @property
    def symbol(self) -> str:
        return "PASS" if self.passed else "FAIL"


@dataclass(frozen=True)
class GateReport:
    """Full result of running the gate: the decision plus every check."""

    status: GateStatus
    approved: bool
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def deploy_ok(self) -> bool:
        return self.status is GateStatus.DEPLOY_OK

    def render(self) -> str:
        """Render a concise, human-readable report."""
        lines = ["PharmaPulse pre-deployment verification gate", ""]
        for check in self.checks:
            lines.append(f"  [{check.symbol}] {check.name}: {check.detail}")
        lines.append("")
        approval_text = "present" if self.approved else "ABSENT"
        lines.append(f"  Architecture-plan approval: {approval_text}")
        lines.append("")
        lines.append(f"  GATE DECISION: {self.status.value}")
        if not self.deploy_ok:
            lines.append(
                "  Deployment is blocked. Resolve the FAIL items above and "
                "pass --approve (or set APPROVE_ARCHITECTURE=1)."
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Lazy boto3 client factories (so import never needs AWS creds/network)
# ---------------------------------------------------------------------------
def _default_bedrock_client(region: str = PRIMARY_REGION) -> Any:
    import boto3  # imported lazily on purpose

    return boto3.client("bedrock", region_name=region)


def _default_route53_client() -> Any:
    import boto3  # imported lazily on purpose

    # Route 53 is a global service; region is irrelevant but required by boto3.
    return boto3.client("route53", region_name=EDGE_REGION)


# ---------------------------------------------------------------------------
# Individual checks — each returns a CheckResult and never raises for an
# expected "not satisfied" condition, so the gate degrades to NO-DEPLOY
# rather than crashing.
# ---------------------------------------------------------------------------
def check_agentcore_docs_consulted(consulted: bool) -> CheckResult:
    """Requirement 19.1 — confirm AgentCore docs were consulted.

    Modelled as a confirmation flag over a documented checklist. The checklist
    URLs are recorded in :data:`AGENTCORE_DOC_CHECKLIST`.
    """
    topics = ", ".join(sorted({topic.split(" ")[0] for topic, _ in AGENTCORE_DOC_CHECKLIST}))
    if consulted:
        detail = (
            f"Confirmed review of {len(AGENTCORE_DOC_CHECKLIST)} AgentCore doc "
            f"references covering {topics}."
        )
        return CheckResult("AgentCore documentation consulted", True, detail)
    return CheckResult(
        "AgentCore documentation consulted",
        False,
        "Not confirmed. Review the AgentCore docs (Runtime, Gateway, Identity, "
        "Memory, Observability, Policy) then pass --docs-consulted "
        "(or set AGENTCORE_DOCS_CONSULTED=1).",
    )


def check_bedrock_model_access(
    bedrock_client: Any,
    model_id: str = TARGET_CLAUDE_SONNET_MODEL,
) -> CheckResult:
    """Requirement 19.2 — verify Bedrock access to the target Claude Sonnet model.

    Lists foundation models via the injected ``bedrock`` client and confirms a
    model whose id/ARN matches the target is present. Inference-profile ids
    (``us.anthropic.claude-sonnet-...``) embed the underlying foundation-model
    id (``anthropic.claude-sonnet-...``); we match on that stable fragment so
    either form of ``model_id`` resolves correctly.
    """
    name = "Bedrock model access (Claude Sonnet)"
    # The foundation-model id is the model id minus any region routing prefix
    # ("us.") that inference profiles add.
    fragment = model_id.split(".", 1)[-1] if model_id.startswith(("us.", "eu.", "apac.")) else model_id
    try:
        response = bedrock_client.list_foundation_models()
    except Exception as exc:  # noqa: BLE001 - surface any AWS/client error as FAIL
        return CheckResult(
            name, False, f"Could not list Bedrock foundation models: {exc}"
        )

    summaries = response.get("modelSummaries", []) if isinstance(response, dict) else []
    for summary in summaries:
        candidate = str(summary.get("modelId", ""))
        if fragment in candidate or candidate in model_id:
            # If the API reports access status, require it to be granted.
            access = summary.get("modelLifecycle", {}).get("status")
            agreement = summary.get("agreementAvailability", {})
            agreement_status = agreement.get("status") if isinstance(agreement, dict) else None
            if agreement_status is not None and agreement_status not in (
                "AVAILABLE",
                "NOT_REQUIRED",
            ):
                return CheckResult(
                    name,
                    False,
                    f"Model '{candidate}' found but access is not granted "
                    f"(agreement status: {agreement_status}). Enable model "
                    "access in the Bedrock console.",
                )
            return CheckResult(
                name,
                True,
                f"Model access confirmed for '{candidate}'"
                + (f" (lifecycle: {access})." if access else "."),
            )

    return CheckResult(
        name,
        False,
        f"Target model '{model_id}' not found among available Bedrock "
        "foundation models in this account/region. Enable model access.",
    )


def check_hosted_zone_exists(
    route53_client: Any,
    zone_name: str = HOSTED_ZONE_NAME,
) -> CheckResult:
    """Requirement 19.3 — confirm the public hosted zone ``awsteach.com`` exists."""
    name = f"Route 53 public hosted zone '{zone_name}'"
    # Route 53 stores zone names with a trailing dot.
    normalized = zone_name if zone_name.endswith(".") else zone_name + "."
    try:
        response = route53_client.list_hosted_zones_by_name(DNSName=zone_name)
    except TypeError:
        # Some fakes/clients only implement list_hosted_zones().
        try:
            response = route53_client.list_hosted_zones()
        except Exception as exc:  # noqa: BLE001
            return CheckResult(name, False, f"Could not list hosted zones: {exc}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, False, f"Could not list hosted zones: {exc}")

    zones = response.get("HostedZones", []) if isinstance(response, dict) else []
    for zone in zones:
        zname = str(zone.get("Name", ""))
        if zname == normalized or zname.rstrip(".") == zone_name.rstrip("."):
            config = zone.get("Config", {}) if isinstance(zone, dict) else {}
            if config.get("PrivateZone", False):
                return CheckResult(
                    name,
                    False,
                    f"A hosted zone '{zone_name}' exists but is PRIVATE; a "
                    "public hosted zone is required.",
                )
            zone_id = zone.get("Id", "")
            return CheckResult(
                name, True, f"Public hosted zone found (id: {zone_id})."
            )

    return CheckResult(
        name,
        False,
        f"Public hosted zone '{zone_name}' not found in this account.",
    )


# ---------------------------------------------------------------------------
# Pure decision functions (no AWS) — the heart of the gate for unit tests
# ---------------------------------------------------------------------------
def aggregate_checks(checks: Sequence[CheckResult]) -> bool:
    """Return True iff every prerequisite check passed."""
    return len(checks) > 0 and all(c.passed for c in checks)


def decide_gate(checks: Sequence[CheckResult], approved: bool) -> GateStatus:
    """Core gating rule (Requirements 19.4, 19.5).

    Returns ``DEPLOY-OK`` **iff** every prerequisite check passed *and* the
    architecture plan is approved. In every other case — any failing check, or
    approval absent — returns ``NO-DEPLOY``.
    """
    if aggregate_checks(checks) and approved:
        return GateStatus.DEPLOY_OK
    return GateStatus.NO_DEPLOY


# ---------------------------------------------------------------------------
# Orchestration — runs the checks and produces a full report
# ---------------------------------------------------------------------------
def run_gate(
    *,
    approved: bool,
    docs_consulted: bool,
    model_id: str = TARGET_CLAUDE_SONNET_MODEL,
    hosted_zone_name: str = HOSTED_ZONE_NAME,
    bedrock_client_factory: Optional[Callable[[], Any]] = None,
    route53_client_factory: Optional[Callable[[], Any]] = None,
) -> GateReport:
    """Run all prerequisite checks and apply the gating decision.

    AWS clients are created lazily through the provided factories (defaulting
    to real boto3 clients) so tests can inject fakes. This function performs no
    deployment — it only gates.
    """
    bedrock_factory = bedrock_client_factory or _default_bedrock_client
    route53_factory = route53_client_factory or _default_route53_client

    checks: List[CheckResult] = [
        check_agentcore_docs_consulted(docs_consulted),
    ]

    # Bedrock model access (19.2) — build the client lazily; any failure to
    # even construct the client becomes a FAIL rather than a crash.
    try:
        bedrock_client = bedrock_factory()
        checks.append(check_bedrock_model_access(bedrock_client, model_id))
    except Exception as exc:  # noqa: BLE001
        checks.append(
            CheckResult(
                "Bedrock model access (Claude Sonnet)",
                False,
                f"Could not create Bedrock client: {exc}",
            )
        )

    # Hosted zone (19.3).
    try:
        route53_client = route53_factory()
        checks.append(check_hosted_zone_exists(route53_client, hosted_zone_name))
    except Exception as exc:  # noqa: BLE001
        checks.append(
            CheckResult(
                f"Route 53 public hosted zone '{hosted_zone_name}'",
                False,
                f"Could not create Route 53 client: {exc}",
            )
        )

    status = decide_gate(checks, approved)
    return GateReport(status=status, approved=approved, checks=checks)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _env_truthy(value: Optional[str]) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"} if value else False


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="predeploy_gate",
        description=(
            "PharmaPulse pre-deployment verification gate (Requirement 19). "
            "Returns DEPLOY-OK only when the AgentCore docs are confirmed "
            "consulted, Bedrock Claude Sonnet access is enabled, the "
            "awsteach.com public hosted zone exists, AND the architecture plan "
            "is approved. This gate does NOT deploy anything."
        ),
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        default=_env_truthy(os.environ.get(ENV_APPROVE)),
        help=(
            "Explicit architecture-plan approval (Req 19.4). Without it the "
            "gate returns NO-DEPLOY. Env alias: APPROVE_ARCHITECTURE=1."
        ),
    )
    parser.add_argument(
        "--docs-consulted",
        action="store_true",
        default=_env_truthy(os.environ.get(ENV_DOCS_CONSULTED)),
        help=(
            "Confirm the AgentCore documentation (Runtime, Gateway, Identity, "
            "Memory, Observability, Policy) was consulted (Req 19.1). "
            "Env alias: AGENTCORE_DOCS_CONSULTED=1."
        ),
    )
    parser.add_argument(
        "--model-id",
        default=TARGET_CLAUDE_SONNET_MODEL,
        help="Target Claude Sonnet model/inference-profile id to verify access for.",
    )
    parser.add_argument(
        "--hosted-zone",
        default=HOSTED_ZONE_NAME,
        help="Public hosted zone name that must exist.",
    )
    parser.add_argument(
        "--region",
        default=PRIMARY_REGION,
        help="AWS region for the Bedrock model-access check.",
    )
    parser.add_argument(
        "--print-docs",
        action="store_true",
        help="Print the AgentCore documentation checklist and exit.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint. Returns 0 on DEPLOY-OK, non-zero on NO-DEPLOY."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.print_docs:
        print("AgentCore documentation checklist (Requirement 19.1):")
        for topic, url in AGENTCORE_DOC_CHECKLIST:
            print(f"  - {topic}: {url}")
        return 0

    report = run_gate(
        approved=args.approve,
        docs_consulted=args.docs_consulted,
        model_id=args.model_id,
        hosted_zone_name=args.hosted_zone,
        bedrock_client_factory=lambda: _default_bedrock_client(args.region),
    )
    print(report.render())
    return 0 if report.deploy_ok else 1


if __name__ == "__main__":
    sys.exit(main())
