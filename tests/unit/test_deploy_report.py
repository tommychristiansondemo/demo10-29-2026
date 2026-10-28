"""Unit tests for scripts/deploy_report.py (Task 20.2).

Covers the pure report-building/rendering logic and the injectable
CloudFormation client path (Requirements 20.1, 20.2, 20.3).
"""

from __future__ import annotations

from scripts.deploy_report import (
    AUTH_STACK_NAME,
    FINAL_URL,
    DeploymentReport,
    SecretLocation,
    build_report,
    extract_outputs,
    fetch_stack_outputs,
    find_secret_location,
    generate_report,
    main,
    render_report,
)
from stacks.config import DEMO_DOMAIN, PRIMARY_REGION


# -- Fakes ------------------------------------------------------------------


class _FakeCfnClient:
    """Minimal injectable stand-in for a boto3 CloudFormation client."""

    def __init__(self, outputs):
        self._outputs = outputs
        self.calls = []

    def describe_stacks(self, StackName):  # noqa: N803 - boto3 param name
        self.calls.append(StackName)
        return {"Stacks": [{"StackName": StackName, "Outputs": self._outputs}]}


_SECRET_ARN = (
    "arn:aws:secretsmanager:us-east-2:123456789012:secret:pharmapulse/demo-AbCdEf"
)
_SECRET_NAME = "pharmapulse/demo-credentials"


def _auth_outputs():
    return [
        {
            "OutputKey": "DemoCredentialsSecretArn",
            "OutputValue": _SECRET_ARN,
            "ExportName": "PharmaPulse-DemoCredentialsSecretArn",
        },
        {
            "OutputKey": "DemoCredentialsSecretName",
            "OutputValue": _SECRET_NAME,
            "ExportName": "PharmaPulse-DemoCredentialsSecretName",
        },
        {
            "OutputKey": "UserPoolId",
            "OutputValue": "us-east-2_ABC123",
            "ExportName": "PharmaPulse-UserPoolId",
        },
    ]


# -- Requirement 20.2: final URL --------------------------------------------


def test_final_url_is_demo_domain_https():
    assert FINAL_URL == f"https://{DEMO_DOMAIN}"
    assert FINAL_URL == "https://demo.awsteach.com"


def test_report_reports_final_url():
    report = build_report({})
    assert report.final_url == "https://demo.awsteach.com"
    assert "https://demo.awsteach.com" in render_report(report)


# -- Requirement 20.3: secret location --------------------------------------


def test_extract_outputs_indexes_key_and_export_name():
    outputs = extract_outputs(_auth_outputs())
    assert outputs["DemoCredentialsSecretArn"] == _SECRET_ARN
    assert outputs["PharmaPulse-DemoCredentialsSecretArn"] == _SECRET_ARN
    assert outputs["DemoCredentialsSecretName"] == _SECRET_NAME


def test_find_secret_location_by_output_key():
    outputs = extract_outputs(_auth_outputs())
    loc = find_secret_location(outputs)
    assert loc.arn == _SECRET_ARN
    assert loc.name == _SECRET_NAME
    assert loc.is_known is True


def test_find_secret_location_by_export_name_only():
    outputs = {
        "PharmaPulse-DemoCredentialsSecretArn": _SECRET_ARN,
        "PharmaPulse-DemoCredentialsSecretName": _SECRET_NAME,
    }
    loc = find_secret_location(outputs)
    assert loc.arn == _SECRET_ARN
    assert loc.name == _SECRET_NAME


def test_secret_location_describe_variants():
    assert SecretLocation(arn="a", name="n").describe() == "n (a)"
    assert SecretLocation(arn="a").describe() == "a"
    assert SecretLocation(name="n").describe() == "n"
    unknown = SecretLocation()
    assert unknown.is_known is False
    assert "not found" in unknown.describe()


def test_report_reports_secret_location():
    outputs = extract_outputs(_auth_outputs())
    report = build_report(outputs)
    rendered = render_report(report)
    assert _SECRET_ARN in rendered
    assert _SECRET_NAME in rendered


# -- extract_outputs edge cases ---------------------------------------------


def test_extract_outputs_handles_empty_and_missing_values():
    assert extract_outputs([]) == {}
    assert extract_outputs([{"OutputKey": "X"}]) == {}  # no OutputValue
    coerced = extract_outputs([{"OutputKey": "N", "OutputValue": 42}])
    assert coerced["N"] == "42"


# -- Injectable client path (Requirement 20.3) ------------------------------


def test_fetch_stack_outputs_uses_injected_client():
    client = _FakeCfnClient(_auth_outputs())
    outputs = fetch_stack_outputs(cfn_client=client)
    assert client.calls == [AUTH_STACK_NAME]
    assert outputs["DemoCredentialsSecretArn"] == _SECRET_ARN


def test_generate_report_end_to_end_with_fake_client():
    client = _FakeCfnClient(_auth_outputs())
    report = generate_report(cfn_client=client)
    assert isinstance(report, DeploymentReport)
    assert report.final_url == "https://demo.awsteach.com"
    assert report.region == PRIMARY_REGION
    assert report.stack_name == AUTH_STACK_NAME
    assert report.secret_location.arn == _SECRET_ARN


# -- Rendering extras + warning ---------------------------------------------


def test_render_includes_other_outputs_but_not_secret_keys():
    outputs = extract_outputs(_auth_outputs())
    rendered = render_report(build_report(outputs))
    assert "UserPoolId" in rendered
    # Secret keys are summarized in the credentials line, not the extras list.
    assert "    - DemoCredentialsSecretArn" not in rendered


def test_render_warns_when_secret_missing():
    rendered = render_report(build_report({}))
    assert "WARNING" in rendered


# -- CLI (non-interactive, Requirement 20.1) --------------------------------


def test_main_reports_url_even_when_cfn_unavailable(capsys):
    # No AWS configured in test env; main must not prompt and must complete.
    exit_code = main(["--stack-name", "DoesNotExist", "--region", "us-east-2"])
    captured = capsys.readouterr()
    # Final URL is always reported even on failure path.
    assert "https://demo.awsteach.com" in captured.out
    assert exit_code in (0, 1)
