"""PharmaPulse post-deployment smoke test (task 20.3).

Exercises the deployed system end-to-end and asserts the demo-critical
invariants so an instructor can confirm, in one command, that a fresh
deployment is wired correctly before class:

* **(Req 18.2)** POSTs a chat prompt to the deployed streaming backend
  (the Lambda Function URL, ``RESPONSE_STREAM``) with a bearer token and
  asserts a streamed Server-Sent-Events (SSE) response comes back.
* **(Req 4.1, 4.5)** Scans the ``pharmapulse-inventory`` DynamoDB table and
  asserts ~200 SKUs across **exactly 3** locations, and that the stored
  items are synthetic-only (no PHI / real-customer fields).
* **(Req 7.1, 9.1)** Reads the agent runtime configuration and asserts it is
  wired to a **Claude Sonnet** Bedrock inference profile with a **Guardrail**
  (id + version) attached.
* **(Req 20.2, 20.3)** Prints the final URL ``https://demo.awsteach.com`` and
  the Secrets Manager location of the demo credentials.

Design notes
------------
Every external dependency (the HTTP transport used to call ``/chat``, the
DynamoDB client, the AgentCore/CloudFormation clients, and the Secrets
Manager lookup) is **injectable**. The module therefore imports and runs its
pure assertion helpers **without AWS or network access**, which lets the
operational tests (task 20.5) drive it with fakes. Real boto3 / httpx clients
are constructed lazily only when the CLI actually runs against a live
deployment.

The pure helpers return :class:`CheckResult` values rather than raising, so a
caller can aggregate every check and report all failures at once. ``main()``
returns a non-zero exit code if any smoke check fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

# Single source of truth for domain, table names, and region (Req 17.4).
from stacks.config import (
    DEMO_DOMAIN,
    PRIMARY_REGION,
    TABLE_INVENTORY,
)

# -- Expectations (from the design / requirements) -----------------------

# "approximately 200 SKUs" (Req 4.1) — accept a tolerance band around 200 so
# the check is robust to the seed generator's exact count.
EXPECTED_SKU_COUNT = 200
SKU_COUNT_MIN = 180
SKU_COUNT_MAX = 220

# Exactly three fictional pharmacy locations (Req 4.1).
EXPECTED_LOCATION_COUNT = 3

# Inventory attributes that are legitimately present (synthetic/public only).
# Composite-key attributes PK/SK are also allowed.
_ALLOWED_INVENTORY_FIELDS = frozenset(
    {
        "PK",
        "SK",
        "sku_id",
        "drug_name",
        "ndc",
        "quantity",
        "reorder_threshold",
        "unit_price",
        "location_id",
    }
)

# Field-name fragments that would indicate PHI or real-customer data. Their
# presence in a stored inventory item fails the synthetic-only check
# (Req 4.5, 21.2, 21.3).
_PHI_FIELD_FRAGMENTS = (
    "patient",
    "customer",
    "ssn",
    "social_security",
    "dob",
    "date_of_birth",
    "birth",
    "mrn",
    "medical_record",
    "diagnosis",
    "prescription",
    "rx_number",
    "email",
    "phone",
    "address",
    "first_name",
    "last_name",
    "full_name",
    "insurance",
)

# A Claude Sonnet inference profile id/ARN contains this fragment (Req 7.1),
# e.g. ``us.anthropic.claude-sonnet-4-20250514-v1:0``.
_CLAUDE_SONNET_FRAGMENT = "claude-sonnet"

# The final demo URL (Req 20.2).
FINAL_URL = f"https://{DEMO_DOMAIN}"


# -- Result type ---------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single smoke check.

    Attributes:
        name:    Short identifier for the check (printed in the report).
        passed:  Whether the check succeeded.
        message: Human-readable detail (success summary or failure reason).
        details: Optional structured values gathered during the check.
    """

    name: str
    passed: bool
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


# -- Pure assertion helpers (no AWS / no network) ------------------------


def check_sse_response(
    status_code: int,
    content_type: Optional[str],
    body: str,
) -> CheckResult:
    """Assert the ``/chat`` response is a successful streamed SSE response.

    A valid streamed response (Req 18.2, 15.3) has a 2xx status, an
    ``text/event-stream`` content type (or SSE ``data:`` frames in the body
    for transports that do not surface the header), and at least one SSE
    event frame.

    Args:
        status_code:  HTTP status returned by the ``/chat`` POST.
        content_type: Value of the response ``Content-Type`` header, if any.
        body:         The (accumulated) response body text.

    Returns:
        A :class:`CheckResult` named ``chat_sse``.
    """
    name = "chat_sse"
    if not 200 <= status_code < 300:
        return CheckResult(
            name,
            False,
            f"/chat returned HTTP {status_code}; expected a 2xx streamed response.",
            {"status_code": status_code},
        )

    ctype = (content_type or "").lower()
    is_event_stream = "text/event-stream" in ctype
    has_sse_frame = "data:" in body
    if not is_event_stream and not has_sse_frame:
        return CheckResult(
            name,
            False,
            (
                "/chat response was not SSE: content-type "
                f"{content_type!r} and no 'data:' frames in the body."
            ),
            {"content_type": content_type},
        )

    return CheckResult(
        name,
        True,
        f"/chat returned a streamed SSE response (HTTP {status_code}).",
        {"status_code": status_code, "content_type": content_type},
    )


def _distinct_locations(items: Sequence[Dict[str, Any]]) -> List[str]:
    """Return the sorted distinct ``location_id`` values across ``items``."""
    seen = set()
    for item in items:
        loc = item.get("location_id")
        if loc is not None:
            seen.add(loc)
    return sorted(seen)


def _phi_fields(item: Dict[str, Any]) -> List[str]:
    """Return any attribute names in ``item`` that look like PHI/customer data."""
    offending: List[str] = []
    for key in item:
        lowered = key.lower()
        if any(fragment in lowered for fragment in _PHI_FIELD_FRAGMENTS):
            offending.append(key)
    return offending


def check_inventory_seed(items: Sequence[Dict[str, Any]]) -> CheckResult:
    """Assert ~200 SKUs across exactly 3 locations, synthetic-only (Req 4.1, 4.5).

    Args:
        items: The scanned inventory rows (plain Python dicts with the
            inventory attributes; DynamoDB type descriptors already removed).

    Returns:
        A :class:`CheckResult` named ``inventory_seed``.
    """
    name = "inventory_seed"
    count = len(items)
    locations = _distinct_locations(items)

    problems: List[str] = []

    if not SKU_COUNT_MIN <= count <= SKU_COUNT_MAX:
        problems.append(
            f"SKU count {count} is outside the expected ~{EXPECTED_SKU_COUNT} "
            f"band [{SKU_COUNT_MIN}, {SKU_COUNT_MAX}]."
        )

    if len(locations) != EXPECTED_LOCATION_COUNT:
        problems.append(
            f"found {len(locations)} distinct locations "
            f"({locations}); expected exactly {EXPECTED_LOCATION_COUNT}."
        )

    # Synthetic-only: no PHI/customer attributes on any stored row (Req 4.5).
    phi_hits: Dict[str, List[str]] = {}
    for item in items:
        offending = _phi_fields(item)
        if offending:
            key = str(item.get("sku_id") or item.get("SK") or "<unknown>")
            phi_hits[key] = offending
    if phi_hits:
        sample = dict(list(phi_hits.items())[:3])
        problems.append(
            f"PHI/customer-like fields found on {len(phi_hits)} item(s); "
            f"sample: {sample}."
        )

    details = {
        "count": count,
        "locations": locations,
        "phi_item_count": len(phi_hits),
    }

    if problems:
        return CheckResult(name, False, " ".join(problems), details)

    return CheckResult(
        name,
        True,
        (
            f"{count} SKUs across exactly {len(locations)} locations "
            f"({locations}); synthetic-only (no PHI/customer fields)."
        ),
        details,
    )


def check_agent_wiring(agent_config: Dict[str, Any]) -> CheckResult:
    """Assert the agent uses a Claude Sonnet profile with a Guardrail (Req 7.1, 9.1).

    Args:
        agent_config: A mapping of the agent runtime's effective configuration,
            typically the runtime's environment variables. Expected keys:
            ``BEDROCK_INFERENCE_PROFILE_ARN``, ``BEDROCK_GUARDRAIL_ID`` and
            optionally ``BEDROCK_GUARDRAIL_VERSION``.

    Returns:
        A :class:`CheckResult` named ``agent_wiring``.
    """
    name = "agent_wiring"
    profile = str(agent_config.get("BEDROCK_INFERENCE_PROFILE_ARN", "") or "")
    guardrail_id = str(agent_config.get("BEDROCK_GUARDRAIL_ID", "") or "")
    guardrail_version = str(
        agent_config.get("BEDROCK_GUARDRAIL_VERSION", "") or ""
    )

    problems: List[str] = []

    if _CLAUDE_SONNET_FRAGMENT not in profile.lower():
        problems.append(
            "inference profile "
            f"{profile!r} does not reference a '{_CLAUDE_SONNET_FRAGMENT}' profile."
        )

    if not guardrail_id:
        problems.append("no Guardrail id is attached (BEDROCK_GUARDRAIL_ID is empty).")

    details = {
        "inference_profile": profile,
        "guardrail_id": guardrail_id,
        "guardrail_version": guardrail_version,
    }

    if problems:
        return CheckResult(name, False, " ".join(problems), details)

    version_note = (
        f" version {guardrail_version}" if guardrail_version else ""
    )
    return CheckResult(
        name,
        True,
        (
            f"agent wired to Claude Sonnet inference profile {profile!r} "
            f"with Guardrail {guardrail_id}{version_note} attached."
        ),
        details,
    )


# -- Transport / client abstractions (injectable) ------------------------


# A chat transport takes (url, prompt, token) and returns
# (status_code, content_type, body). Defaults to httpx (lazy import).
ChatTransport = Callable[[str, str, str], "tuple[int, Optional[str], str]"]


def _default_chat_transport(
    url: str, prompt: str, token: str
) -> "tuple[int, Optional[str], str]":
    """POST a chat prompt to the backend Function URL and read the SSE stream.

    Lazily imports ``httpx`` so the module imports without it. Streams the
    response so the SSE frames are accumulated into the returned body.
    """
    import httpx  # Local import keeps the module importable without httpx.

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload = {"prompt": prompt}
    chunks: List[str] = []
    with httpx.Client(timeout=60.0) as client:
        with client.stream(
            "POST", url, headers=headers, json=payload
        ) as response:
            content_type = response.headers.get("content-type")
            for text in response.iter_text():
                chunks.append(text)
                # One SSE frame is enough to confirm streaming; stop early.
                if "data:" in "".join(chunks):
                    break
            return response.status_code, content_type, "".join(chunks)


def exercise_chat_endpoint(
    backend_url: str,
    token: str,
    *,
    prompt: str = "Is amoxicillin in shortage, and do we have stock?",
    transport: Optional[ChatTransport] = None,
) -> CheckResult:
    """Call ``/chat`` on the backend and assert a streamed SSE response (Req 18.2).

    Args:
        backend_url: The streaming backend Function URL.
        token:       Bearer token (Cognito JWT) to authorize the request.
        prompt:      The chat prompt to send (a comparison prompt by default,
            which exercises both tool families — Req 7.4).
        transport:   Injectable callable ``(url, prompt, token) ->
            (status, content_type, body)``. Defaults to an httpx-based
            transport constructed lazily.

    Returns:
        A :class:`CheckResult` named ``chat_sse``.
    """
    transport = transport or _default_chat_transport
    try:
        status_code, content_type, body = transport(backend_url, prompt, token)
    except Exception as exc:  # noqa: BLE001 - surface any transport failure as a check failure.
        return CheckResult(
            "chat_sse",
            False,
            f"/chat request raised {type(exc).__name__}: {exc}",
            {"error": str(exc)},
        )
    return check_sse_response(status_code, content_type, body)


# A DynamoDB scanner returns the inventory rows as plain dicts. Defaults to a
# boto3 resource-table scan (lazy import).
InventoryScanner = Callable[[], List[Dict[str, Any]]]


def _default_inventory_scanner(
    table_name: str = TABLE_INVENTORY,
    region: str = PRIMARY_REGION,
) -> List[Dict[str, Any]]:
    """Scan the inventory table and return rows as plain dicts (lazy boto3)."""
    import boto3  # Local import keeps the module importable without boto3.

    resource = boto3.resource("dynamodb", region_name=region)
    table = resource.Table(table_name)
    items: List[Dict[str, Any]] = []
    scan_kwargs: Dict[str, Any] = {}
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return items


def verify_inventory_seed(
    *,
    scanner: Optional[InventoryScanner] = None,
    items: Optional[Sequence[Dict[str, Any]]] = None,
) -> CheckResult:
    """Verify the seeded inventory (Req 4.1, 4.5).

    Either an already-scanned ``items`` sequence or a ``scanner`` callable may
    be supplied; if neither is given a default boto3 scan is used.

    Args:
        scanner: Injectable callable returning the inventory rows.
        items:   Pre-fetched inventory rows (takes precedence over ``scanner``).

    Returns:
        A :class:`CheckResult` named ``inventory_seed``.
    """
    if items is None:
        scanner = scanner or _default_inventory_scanner
        try:
            items = scanner()
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                "inventory_seed",
                False,
                f"inventory scan raised {type(exc).__name__}: {exc}",
                {"error": str(exc)},
            )
    return check_inventory_seed(list(items))


# An agent-config reader returns the agent runtime's effective config
# (its environment). Defaults to reading CloudFormation stack outputs via boto3.
AgentConfigReader = Callable[[], Dict[str, Any]]


def _default_agent_config_reader(
    region: str = PRIMARY_REGION,
) -> Dict[str, Any]:
    """Read the agent wiring from the AgentStack CloudFormation outputs (lazy boto3).

    The AgentStack exports the guardrail id/arn as stack outputs; the inference
    profile is injected into the runtime env. This reader surfaces the
    guardrail id from the stack outputs and the inference profile from the
    runtime's environment via the AgentCore control-plane describe call,
    falling back to stack outputs where available.
    """
    import boto3  # Local import keeps the module importable without boto3.

    config: Dict[str, Any] = {}

    # Guardrail id/version come from the AgentStack CloudFormation outputs.
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        described = cfn.describe_stacks(StackName="PharmaPulse-Agent")
        outputs = described["Stacks"][0].get("Outputs", [])
        for output in outputs:
            if output.get("OutputKey") == "AgentGuardrailId":
                config["BEDROCK_GUARDRAIL_ID"] = output.get("OutputValue", "")
    except Exception:  # noqa: BLE001 - tolerate missing stack; check will fail cleanly.
        pass

    # Inference profile + guardrail version come from the runtime environment.
    try:
        control = boto3.client("bedrock-agentcore-control", region_name=region)
        runtimes = control.list_agent_runtimes().get("agentRuntimes", [])
        for runtime in runtimes:
            runtime_id = runtime.get("agentRuntimeId") or runtime.get("id")
            if runtime_id is None:
                continue
            described_rt = control.get_agent_runtime(agentRuntimeId=runtime_id)
            env = (
                described_rt.get("agentRuntimeArtifact", {})
                .get("containerConfiguration", {})
                .get("environmentVariables", {})
            )
            if "BEDROCK_INFERENCE_PROFILE_ARN" in env:
                config.setdefault(
                    "BEDROCK_INFERENCE_PROFILE_ARN",
                    env.get("BEDROCK_INFERENCE_PROFILE_ARN", ""),
                )
                config.setdefault(
                    "BEDROCK_GUARDRAIL_ID", env.get("BEDROCK_GUARDRAIL_ID", "")
                )
                config.setdefault(
                    "BEDROCK_GUARDRAIL_VERSION",
                    env.get("BEDROCK_GUARDRAIL_VERSION", ""),
                )
                break
    except Exception:  # noqa: BLE001 - tolerate control-plane differences; check fails cleanly.
        pass

    return config


def verify_agent_wiring(
    *,
    reader: Optional[AgentConfigReader] = None,
    agent_config: Optional[Dict[str, Any]] = None,
) -> CheckResult:
    """Verify the agent is wired to Claude Sonnet + Guardrail (Req 7.1, 9.1).

    Args:
        reader:       Injectable callable returning the agent config mapping.
        agent_config: Pre-fetched config (takes precedence over ``reader``).

    Returns:
        A :class:`CheckResult` named ``agent_wiring``.
    """
    if agent_config is None:
        reader = reader or _default_agent_config_reader
        try:
            agent_config = reader()
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                "agent_wiring",
                False,
                f"reading agent config raised {type(exc).__name__}: {exc}",
                {"error": str(exc)},
            )
    return check_agent_wiring(agent_config)


# A secrets-location resolver returns the Secrets Manager ARN/name holding the
# demo credentials. Defaults to reading the AuthStack CloudFormation output.
SecretsLocationResolver = Callable[[], str]


def _default_secrets_location_resolver(
    region: str = PRIMARY_REGION,
) -> str:
    """Resolve the demo-credentials Secrets Manager location from AuthStack outputs."""
    import boto3  # Local import keeps the module importable without boto3.

    cfn = boto3.client("cloudformation", region_name=region)
    described = cfn.describe_stacks(StackName="PharmaPulse-Auth")
    outputs = described["Stacks"][0].get("Outputs", [])
    for output in outputs:
        key = (output.get("OutputKey") or "").lower()
        if "secret" in key:
            return output.get("OutputValue", "")
    return ""


def resolve_secrets_location(
    *,
    resolver: Optional[SecretsLocationResolver] = None,
    location: Optional[str] = None,
) -> str:
    """Return the Secrets Manager location of the demo credentials (Req 20.3).

    Args:
        resolver: Injectable callable returning the Secrets Manager location.
        location: Pre-resolved location (takes precedence over ``resolver``).

    Returns:
        The Secrets Manager ARN/name, or an empty string if it cannot be
        resolved (the printout then notes it was unavailable).
    """
    if location is not None:
        return location
    resolver = resolver or _default_secrets_location_resolver
    try:
        return resolver()
    except Exception:  # noqa: BLE001 - absence is reported, not fatal, for the printout.
        return ""


# -- Orchestration -------------------------------------------------------


def run_smoke_checks(
    *,
    backend_url: str,
    token: str,
    chat_transport: Optional[ChatTransport] = None,
    inventory_scanner: Optional[InventoryScanner] = None,
    inventory_items: Optional[Sequence[Dict[str, Any]]] = None,
    agent_config_reader: Optional[AgentConfigReader] = None,
    agent_config: Optional[Dict[str, Any]] = None,
) -> List[CheckResult]:
    """Run every smoke check and return their results (no printing/exit).

    All dependencies are injectable so this is fully testable with fakes.
    """
    return [
        exercise_chat_endpoint(
            backend_url, token, transport=chat_transport
        ),
        verify_inventory_seed(
            scanner=inventory_scanner, items=inventory_items
        ),
        verify_agent_wiring(
            reader=agent_config_reader, agent_config=agent_config
        ),
    ]


def format_report(
    results: Iterable[CheckResult],
    *,
    secrets_location: str,
    final_url: str = FINAL_URL,
) -> str:
    """Build the printable smoke-test report, including the URL + secrets (Req 20.2, 20.3)."""
    lines: List[str] = ["PharmaPulse smoke test", "=" * 40]
    for result in results:
        marker = "PASS" if result.passed else "FAIL"
        lines.append(f"[{marker}] {result.name}: {result.message}")
    lines.append("-" * 40)
    lines.append(f"Final URL:            {final_url}")
    lines.append(
        "Demo credentials in:  "
        + (secrets_location or "<unavailable: could not resolve Secrets Manager location>")
    )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint. Returns a non-zero exit code if any smoke check fails."""
    parser = argparse.ArgumentParser(
        prog="smoke_test",
        description=(
            "PharmaPulse post-deployment smoke test: exercises /chat, verifies "
            "the inventory seed (~200 SKUs / 3 locations / synthetic-only), and "
            "the agent's Claude Sonnet + Guardrail wiring, then prints the final "
            "URL and demo-credentials Secrets Manager location."
        ),
    )
    parser.add_argument(
        "--backend-url",
        help="Streaming backend Function URL to POST /chat to.",
    )
    parser.add_argument(
        "--token",
        help="Bearer token (Cognito JWT) used to authorize the /chat request.",
    )
    parser.add_argument(
        "--region",
        default=PRIMARY_REGION,
        help=f"AWS region for the deployment (default: {PRIMARY_REGION}).",
    )
    parser.add_argument(
        "--skip-chat",
        action="store_true",
        help="Skip the live /chat SSE check (useful when only verifying data).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the results as JSON instead of the text report.",
    )
    args = parser.parse_args(argv)

    results: List[CheckResult] = []

    if not args.skip_chat:
        if not args.backend_url or not args.token:
            print(
                "error: --backend-url and --token are required unless --skip-chat "
                "is set.",
                file=sys.stderr,
            )
            return 2
        results.append(
            exercise_chat_endpoint(args.backend_url, args.token)
        )

    results.append(verify_inventory_seed())
    results.append(verify_agent_wiring())

    secrets_location = resolve_secrets_location()

    if args.json:
        payload = {
            "final_url": FINAL_URL,
            "secrets_location": secrets_location,
            "checks": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "message": r.message,
                    "details": r.details,
                }
                for r in results
            ],
            "passed": all(r.passed for r in results),
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(format_report(results, secrets_location=secrets_location))

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
