"""Deployment reporting output for PharmaPulse (Task 20.2).

On deployment completion this module reports:

* The final URL ``https://demo.awsteach.com`` (Requirement 20.2), derived
  from :data:`stacks.config.DEMO_DOMAIN`.
* The Secrets Manager location of the demo credentials (Requirement 20.3),
  read from the CloudFormation stack outputs that the auth stack exports
  (``PharmaPulse-DemoCredentialsSecretArn`` /
  ``PharmaPulse-DemoCredentialsSecretName``).
* Any other key stack outputs that are useful for the instructor.

The reporter runs **non-interactively** (Requirement 20.1): it never prompts
for input and always runs to completion, printing a clean, readable summary.

Design notes for testability:

* The pure functions (:func:`build_report`, :func:`render_report`,
  :func:`extract_outputs`, :func:`find_secret_location`) take plain data and
  have no AWS dependency, so they are unit-testable without credentials.
* The AWS CloudFormation client is **injectable** and defaults to ``boto3``
  lazily inside :func:`fetch_stack_outputs`, so importing this module never
  requires AWS to be configured.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from stacks.config import DEMO_DOMAIN, PRIMARY_REGION

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default CloudFormation stack that exports the demo credentials location.
AUTH_STACK_NAME = "PharmaPulse-Auth"

#: Final URL reported on deployment completion (Requirement 20.2).
FINAL_URL = f"https://{DEMO_DOMAIN}"

#: Output keys exported by the auth stack for the demo credentials secret
#: (Requirement 20.3). Order matters: the ARN is the most precise location.
SECRET_ARN_OUTPUT_KEY = "DemoCredentialsSecretArn"
SECRET_NAME_OUTPUT_KEY = "DemoCredentialsSecretName"

#: Export names (used when outputs are keyed by export name instead of the
#: logical output id).
SECRET_ARN_EXPORT_NAME = "PharmaPulse-DemoCredentialsSecretArn"
SECRET_NAME_EXPORT_NAME = "PharmaPulse-DemoCredentialsSecretName"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretLocation:
    """The Secrets Manager location of the demo credentials."""

    arn: Optional[str] = None
    name: Optional[str] = None

    @property
    def is_known(self) -> bool:
        """True when at least an ARN or a name was resolved."""
        return bool(self.arn or self.name)

    def describe(self) -> str:
        """Human-readable description of the secret location."""
        if self.arn and self.name:
            return f"{self.name} ({self.arn})"
        if self.arn:
            return self.arn
        if self.name:
            return self.name
        return "<not found in stack outputs>"


@dataclass(frozen=True)
class DeploymentReport:
    """The data structure reported on deployment completion."""

    final_url: str
    secret_location: SecretLocation
    region: str
    stack_name: str
    stack_outputs: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure helpers (no AWS dependency)
# ---------------------------------------------------------------------------


def extract_outputs(raw_outputs: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Normalize CloudFormation ``Outputs`` into a flat ``key -> value`` map.

    Accepts the ``Stacks[].Outputs`` shape returned by
    ``cloudformation:DescribeStacks`` where each output is a mapping with
    ``OutputKey``, ``OutputValue`` and optionally ``ExportName``. Both the
    logical ``OutputKey`` and the ``ExportName`` are indexed so lookups work
    with either, and non-string values are coerced to ``str``.
    """
    outputs: dict[str, str] = {}
    for item in raw_outputs or []:
        value = item.get("OutputValue")
        if value is None:
            continue
        value = str(value)
        key = item.get("OutputKey")
        if key:
            outputs[str(key)] = value
        export_name = item.get("ExportName")
        if export_name:
            outputs[str(export_name)] = value
    return outputs


def find_secret_location(outputs: Mapping[str, str]) -> SecretLocation:
    """Resolve the demo credentials secret location from stack outputs.

    Looks up both the logical output keys and the export names so callers can
    pass either shape (Requirement 20.3).
    """
    arn = outputs.get(SECRET_ARN_OUTPUT_KEY) or outputs.get(SECRET_ARN_EXPORT_NAME)
    name = outputs.get(SECRET_NAME_OUTPUT_KEY) or outputs.get(SECRET_NAME_EXPORT_NAME)
    return SecretLocation(arn=arn, name=name)


def build_report(
    outputs: Mapping[str, str],
    *,
    final_url: str = FINAL_URL,
    region: str = PRIMARY_REGION,
    stack_name: str = AUTH_STACK_NAME,
) -> DeploymentReport:
    """Build the :class:`DeploymentReport` from normalized stack outputs."""
    return DeploymentReport(
        final_url=final_url,
        secret_location=find_secret_location(outputs),
        region=region,
        stack_name=stack_name,
        stack_outputs=dict(outputs),
    )


def render_report(report: DeploymentReport) -> str:
    """Render a readable, non-interactive summary of the deployment."""
    lines = [
        "=" * 64,
        "PharmaPulse deployment complete",
        "=" * 64,
        f"  Final URL          : {report.final_url}",
        f"  Demo credentials   : {report.secret_location.describe()}",
        f"  Secrets Manager via: {report.stack_name} ({report.region})",
    ]

    if not report.secret_location.is_known:
        lines.append(
            "  WARNING            : demo credentials secret location was not "
            "found in the stack outputs."
        )

    # Surface any other key stack outputs for operator convenience.
    extras = {
        key: value
        for key, value in sorted(report.stack_outputs.items())
        if key
        not in {
            SECRET_ARN_OUTPUT_KEY,
            SECRET_NAME_OUTPUT_KEY,
            SECRET_ARN_EXPORT_NAME,
            SECRET_NAME_EXPORT_NAME,
        }
    }
    if extras:
        lines.append("-" * 64)
        lines.append("  Other stack outputs:")
        for key, value in extras.items():
            lines.append(f"    - {key}: {value}")

    lines.append("=" * 64)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AWS access (injectable, lazy boto3)
# ---------------------------------------------------------------------------


def _default_cfn_client(region: str):
    """Create a default boto3 CloudFormation client lazily.

    Imported inside the function so this module imports without boto3/AWS
    being configured, keeping the pure helpers testable in isolation.
    """
    import boto3  # noqa: WPS433 (intentional lazy import)

    return boto3.client("cloudformation", region_name=region)


def fetch_stack_outputs(
    *,
    stack_name: str = AUTH_STACK_NAME,
    region: str = PRIMARY_REGION,
    cfn_client: Any = None,
) -> dict[str, str]:
    """Fetch and normalize the outputs of a CloudFormation stack.

    The ``cfn_client`` is injectable for testing; when omitted a boto3
    CloudFormation client is created lazily (Requirement 20.3). Returns a
    normalized ``key -> value`` map via :func:`extract_outputs`.
    """
    client = cfn_client or _default_cfn_client(region)
    response = client.describe_stacks(StackName=stack_name)
    stacks = response.get("Stacks", []) if isinstance(response, Mapping) else []
    raw_outputs: list[Mapping[str, Any]] = []
    for stack in stacks:
        raw_outputs.extend(stack.get("Outputs", []) or [])
    return extract_outputs(raw_outputs)


def generate_report(
    *,
    stack_name: str = AUTH_STACK_NAME,
    region: str = PRIMARY_REGION,
    cfn_client: Any = None,
) -> DeploymentReport:
    """Fetch stack outputs and build the deployment report."""
    outputs = fetch_stack_outputs(
        stack_name=stack_name, region=region, cfn_client=cfn_client
    )
    return build_report(
        outputs, final_url=FINAL_URL, region=region, stack_name=stack_name
    )


# ---------------------------------------------------------------------------
# CLI entrypoint (non-interactive, Requirement 20.1)
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="deploy_report",
        description=(
            "Report the PharmaPulse deployment: final URL and the Secrets "
            "Manager location of the demo credentials. Runs non-interactively."
        ),
    )
    parser.add_argument(
        "--stack-name",
        default=AUTH_STACK_NAME,
        help="CloudFormation stack exporting the demo credentials secret.",
    )
    parser.add_argument(
        "--region",
        default=PRIMARY_REGION,
        help="AWS region of the CloudFormation stack.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint. Prints the deployment summary without prompting."""
    args = _parse_args(argv)
    try:
        report = generate_report(stack_name=args.stack_name, region=args.region)
    except Exception as exc:  # noqa: BLE001 - report cleanly, never prompt
        # Even on failure to reach CloudFormation, report what we know
        # (the final URL) so the step completes non-interactively.
        fallback = build_report({}, region=args.region, stack_name=args.stack_name)
        print(render_report(fallback))
        print(f"ERROR: could not read stack outputs: {exc}", file=sys.stderr)
        return 1
    print(render_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
