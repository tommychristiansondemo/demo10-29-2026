"""Operational-script tests (Task 20.5).

Covers two operational scripts with fakes only (no AWS):

* ``scripts/teardown.py`` (Requirement 18.5) — teardown must ABORT on the
  first non-hosted-zone stack delete failure, record the failing stack + a
  reason in the report, leave ``report.ok`` False, never touch the protected
  ``awsteach.com`` hosted zone, and make ``main()`` return non-zero. A clean
  run deletes every stack and reports ``ok`` True.
* ``scripts/predeploy_gate.py`` (Requirements 19.2, 19.3, 19.4, 19.5) — the
  gate returns NO-DEPLOY unless every prerequisite check passes AND approval
  is present. Bedrock model access (19.2) and hosted-zone existence (19.3) are
  checked against fake clients; the pure decision functions are unit-tested
  directly.
"""

from __future__ import annotations

import pytest

from scripts.predeploy_gate import (
    TARGET_CLAUDE_SONNET_MODEL,
    CheckResult,
    GateStatus,
    aggregate_checks,
    check_bedrock_model_access,
    check_hosted_zone_exists,
    decide_gate,
    run_gate,
)
from scripts.teardown import (
    DEPLOY_ORDER,
    PROTECTED_HOSTED_ZONE,
    StackResult,
    TeardownError,
    delete_stack,
    is_protected_hosted_zone,
    main as teardown_main,
    region_for_stack,
    run_teardown,
    teardown_order,
)
from stacks.config import HOSTED_ZONE_NAME


# ===========================================================================
# Teardown fakes
# ===========================================================================
class _FakeWaiter:
    """A CloudFormation waiter that optionally raises for a target stack."""

    def __init__(self, fail_stack=None, exc=None):
        self._fail_stack = fail_stack
        self._exc = exc

    def wait(self, *, StackName):  # noqa: N803 - boto3 param name
        if self._fail_stack is not None and StackName == self._fail_stack:
            raise self._exc


class _FakeCfnClient:
    """Minimal injectable CloudFormation client.

    * ``delete_stack`` records the call and (optionally) raises for a target
      stack to simulate a delete API failure.
    * ``get_waiter`` returns a waiter that (optionally) raises for a target
      stack to simulate a delete that never completes / rolls back.
    * ``describe_stacks`` reports every stack as existing (CREATE_COMPLETE) so
      teardown actually attempts a delete, and provides a status reason used
      to enrich the failure message.
    """

    def __init__(self, *, fail_stack=None, fail_mode="waiter", exc=None):
        self.fail_stack = fail_stack
        self.fail_mode = fail_mode
        self.exc = exc or RuntimeError("DELETE_FAILED: resource stuck")
        self.deleted = []  # stacks for which delete_stack was invoked
        self.describe_calls = []

    def delete_stack(self, *, StackName):  # noqa: N803
        self.deleted.append(StackName)
        if (
            self.fail_stack is not None
            and StackName == self.fail_stack
            and self.fail_mode == "delete"
        ):
            raise self.exc
        return {}

    def get_waiter(self, waiter_name):
        if self.fail_mode == "waiter":
            return _FakeWaiter(fail_stack=self.fail_stack, exc=self.exc)
        return _FakeWaiter()

    def describe_stacks(self, *, StackName):  # noqa: N803
        self.describe_calls.append(StackName)
        return {
            "Stacks": [
                {
                    "StackName": StackName,
                    "StackStatus": "DELETE_FAILED"
                    if StackName == self.fail_stack
                    else "CREATE_COMPLETE",
                    "StackStatusReason": "resource X could not be deleted"
                    if StackName == self.fail_stack
                    else "",
                }
            ]
        }


def _factory_returning(client):
    """A client_factory that returns the same fake for every region."""

    def factory(_region):
        return client

    return factory


# ===========================================================================
# Teardown: pure helpers
# ===========================================================================
def test_teardown_order_is_reverse_of_deploy_order():
    order = teardown_order()
    assert order == list(reversed(list(DEPLOY_ORDER)))
    # Foundation is deployed first, so it is torn down last.
    assert order[-1] == "PharmaPulse-Foundation"


def test_region_for_stack_places_edge_stacks_in_us_east_1():
    assert region_for_stack("PharmaPulse-EdgeCert") == "us-east-1"
    assert region_for_stack("PharmaPulse-Foundation") == "us-east-2"


def test_is_protected_hosted_zone_matches_bare_and_fqdn():
    assert is_protected_hosted_zone("awsteach.com") is True
    assert is_protected_hosted_zone("awsteach.com.") is True
    assert is_protected_hosted_zone("AWSTEACH.COM") is True
    assert is_protected_hosted_zone("example.com") is False
    assert is_protected_hosted_zone("") is False
    assert PROTECTED_HOSTED_ZONE == HOSTED_ZONE_NAME


# ===========================================================================
# Teardown: Requirement 18.5 — abort + report on a delete failure
# ===========================================================================
def test_run_teardown_aborts_on_waiter_failure_and_reports(capsys):
    order = teardown_order()
    # Pick a stack in the MIDDLE of the teardown order so we can prove the run
    # stops before reaching stacks ordered after it.
    fail_stack = order[3]
    stacks_after = order[4:]

    exc = RuntimeError("Waiter StackDeleteComplete failed: DELETE_FAILED")
    client = _FakeCfnClient(fail_stack=fail_stack, fail_mode="waiter", exc=exc)

    report = run_teardown(client_factory=_factory_returning(client))

    # The run is not OK and records the failing stack with a reason (18.5).
    assert report.ok is False
    assert report.aborted is True
    assert report.failure is not None
    assert report.failure.stack_name == fail_stack
    assert report.failure.status == "failed"
    assert report.failure.reason  # a non-empty, human-readable reason
    # The reason should name why it failed (enriched from describe_stacks).
    assert "resource X could not be deleted" in report.failure.reason

    # ABORT: no stack ordered AFTER the failing one is deleted.
    for later in stacks_after:
        assert later not in client.deleted
    # The failing stack itself was attempted.
    assert fail_stack in client.deleted

    # A human-readable abort message is emitted.
    out = capsys.readouterr().out
    assert "abort" in out.lower()
    assert fail_stack in out


def test_run_teardown_aborts_on_delete_api_failure():
    order = teardown_order()
    fail_stack = order[1]
    client = _FakeCfnClient(
        fail_stack=fail_stack,
        fail_mode="delete",
        exc=RuntimeError("AccessDenied: not authorized to delete"),
    )

    report = run_teardown(client_factory=_factory_returning(client))

    assert report.ok is False
    assert report.failure is not None
    assert report.failure.stack_name == fail_stack
    assert "AccessDenied" in report.failure.reason
    # Nothing past the failure point was attempted.
    for later in order[2:]:
        assert later not in client.deleted


def test_run_teardown_success_deletes_every_stack():
    client = _FakeCfnClient()  # no failures
    report = run_teardown(client_factory=_factory_returning(client))

    assert report.ok is True
    assert report.aborted is False
    assert report.failure is None
    # Every stack in the deploy order was deleted exactly once.
    assert set(client.deleted) == set(DEPLOY_ORDER)
    assert len(client.deleted) == len(DEPLOY_ORDER)
    assert all(r.status == "deleted" for r in report.results)


def test_run_teardown_never_deletes_protected_hosted_zone():
    client = _FakeCfnClient()
    run_teardown(client_factory=_factory_returning(client))

    # The protected hosted zone is never the subject of a delete call.
    for deleted in client.deleted:
        assert not is_protected_hosted_zone(deleted)
    assert HOSTED_ZONE_NAME not in client.deleted

    # delete_stack guards against the hosted zone directly: even if a stack
    # name resolved to it, no delete is issued and it is reported "skipped".
    result = delete_stack(client, HOSTED_ZONE_NAME, region="us-east-1")
    assert result.status == "skipped"
    assert HOSTED_ZONE_NAME not in client.deleted


def test_delete_stack_raises_teardown_error_with_result_on_failure():
    client = _FakeCfnClient(
        fail_stack="PharmaPulse-Data", fail_mode="waiter",
    )
    with pytest.raises(TeardownError) as excinfo:
        delete_stack(client, "PharmaPulse-Data", region="us-east-2")
    result = excinfo.value.result
    assert isinstance(result, StackResult)
    assert result.stack_name == "PharmaPulse-Data"
    assert result.status == "failed"
    assert result.reason


# ===========================================================================
# Teardown: main() exit code (Requirement 18.5)
# ===========================================================================
def test_teardown_main_dry_run_returns_zero():
    # --dry-run deletes nothing and succeeds.
    assert teardown_main(["--dry-run"]) == 0


def test_teardown_main_returns_nonzero_on_failure(monkeypatch):
    order = teardown_order()
    fail_stack = order[0]
    client = _FakeCfnClient(fail_stack=fail_stack, fail_mode="waiter")

    # Patch the default client factory so main() (which does not accept an
    # injected factory) uses our failing fake.
    monkeypatch.setattr(
        "scripts.teardown._default_client_factory",
        _factory_returning(client),
    )
    assert teardown_main([]) == 1


def test_teardown_main_returns_zero_on_success(monkeypatch):
    client = _FakeCfnClient()
    monkeypatch.setattr(
        "scripts.teardown._default_client_factory",
        _factory_returning(client),
    )
    assert teardown_main([]) == 0


# ===========================================================================
# Predeploy gate fakes
# ===========================================================================
class _FakeBedrockClient:
    """Fake bedrock client whose list_foundation_models reports the target
    Claude Sonnet model present/granted or absent."""

    def __init__(self, *, present=True, agreement_status="AVAILABLE"):
        self.present = present
        self.agreement_status = agreement_status

    def list_foundation_models(self):
        summaries = []
        if self.present:
            summary = {
                "modelId": "anthropic.claude-sonnet-4-20250514-v1:0",
                "modelLifecycle": {"status": "ACTIVE"},
            }
            if self.agreement_status is not None:
                summary["agreementAvailability"] = {
                    "status": self.agreement_status
                }
            summaries.append(summary)
        # Always include an unrelated model to ensure matching is specific.
        summaries.append({"modelId": "amazon.titan-text-express-v1"})
        return {"modelSummaries": summaries}


class _FakeRoute53Client:
    """Fake route53 client whose list_hosted_zones_by_name reports the
    awsteach.com public hosted zone present or absent."""

    def __init__(self, *, present=True, private=False):
        self.present = present
        self.private = private

    def list_hosted_zones_by_name(self, DNSName):  # noqa: N803
        zones = []
        if self.present:
            zones.append(
                {
                    "Name": f"{HOSTED_ZONE_NAME}.",
                    "Id": "/hostedzone/Z123EXAMPLE",
                    "Config": {"PrivateZone": self.private},
                }
            )
        return {"HostedZones": zones}


def _bedrock_factory(**kwargs):
    return lambda: _FakeBedrockClient(**kwargs)


def _route53_factory(**kwargs):
    return lambda: _FakeRoute53Client(**kwargs)


# ===========================================================================
# Predeploy gate: individual checks (19.2, 19.3)
# ===========================================================================
def test_check_bedrock_model_access_present_passes():
    result = check_bedrock_model_access(_FakeBedrockClient(present=True))
    assert result.passed is True


def test_check_bedrock_model_access_absent_fails():
    result = check_bedrock_model_access(_FakeBedrockClient(present=False))
    assert result.passed is False
    assert "not found" in result.detail.lower()


def test_check_bedrock_model_access_gated_fails():
    result = check_bedrock_model_access(
        _FakeBedrockClient(present=True, agreement_status="NOT_AVAILABLE")
    )
    assert result.passed is False
    assert "access is not granted" in result.detail.lower()


def test_check_hosted_zone_exists_present_passes():
    result = check_hosted_zone_exists(_FakeRoute53Client(present=True))
    assert result.passed is True


def test_check_hosted_zone_absent_fails():
    result = check_hosted_zone_exists(_FakeRoute53Client(present=False))
    assert result.passed is False
    assert "not found" in result.detail.lower()


def test_check_hosted_zone_private_fails():
    result = check_hosted_zone_exists(
        _FakeRoute53Client(present=True, private=True)
    )
    assert result.passed is False
    assert "private" in result.detail.lower()


# ===========================================================================
# Predeploy gate: pure decision functions (19.4, 19.5)
# ===========================================================================
def _passing(name="x"):
    return CheckResult(name, True, "ok")


def _failing(name="x"):
    return CheckResult(name, False, "nope")


def test_aggregate_checks_requires_all_pass_and_nonempty():
    assert aggregate_checks([_passing("a"), _passing("b")]) is True
    assert aggregate_checks([_passing("a"), _failing("b")]) is False
    assert aggregate_checks([]) is False


def test_decide_gate_deploy_ok_only_when_all_pass_and_approved():
    checks = [_passing("a"), _passing("b")]
    assert decide_gate(checks, approved=True) is GateStatus.DEPLOY_OK


def test_decide_gate_no_deploy_when_approval_absent():
    checks = [_passing("a"), _passing("b")]
    assert decide_gate(checks, approved=False) is GateStatus.NO_DEPLOY


def test_decide_gate_no_deploy_when_any_check_fails():
    checks = [_passing("a"), _failing("b")]
    assert decide_gate(checks, approved=True) is GateStatus.NO_DEPLOY
    assert decide_gate(checks, approved=False) is GateStatus.NO_DEPLOY


# ===========================================================================
# Predeploy gate: end-to-end run_gate (19.2 - 19.5)
# ===========================================================================
def test_run_gate_no_deploy_when_approval_absent_even_if_prereqs_pass():
    report = run_gate(
        approved=False,
        docs_consulted=True,
        bedrock_client_factory=_bedrock_factory(present=True),
        route53_client_factory=_route53_factory(present=True),
    )
    assert report.status is GateStatus.NO_DEPLOY
    assert report.deploy_ok is False
    # All prerequisite checks themselves passed; only approval is missing.
    assert all(c.passed for c in report.checks)


def test_run_gate_deploy_ok_when_all_prereqs_pass_and_approved():
    report = run_gate(
        approved=True,
        docs_consulted=True,
        bedrock_client_factory=_bedrock_factory(present=True),
        route53_client_factory=_route53_factory(present=True),
    )
    assert report.status is GateStatus.DEPLOY_OK
    assert report.deploy_ok is True


def test_run_gate_no_deploy_when_model_access_absent():
    report = run_gate(
        approved=True,
        docs_consulted=True,
        bedrock_client_factory=_bedrock_factory(present=False),
        route53_client_factory=_route53_factory(present=True),
    )
    assert report.status is GateStatus.NO_DEPLOY


def test_run_gate_no_deploy_when_hosted_zone_absent():
    report = run_gate(
        approved=True,
        docs_consulted=True,
        bedrock_client_factory=_bedrock_factory(present=True),
        route53_client_factory=_route53_factory(present=False),
    )
    assert report.status is GateStatus.NO_DEPLOY


def test_run_gate_no_deploy_when_docs_not_consulted():
    report = run_gate(
        approved=True,
        docs_consulted=False,
        bedrock_client_factory=_bedrock_factory(present=True),
        route53_client_factory=_route53_factory(present=True),
    )
    assert report.status is GateStatus.NO_DEPLOY


def test_run_gate_target_model_id_is_claude_sonnet():
    # Guard against drift: the gate targets the Claude Sonnet model the agent
    # actually runs on.
    assert "claude-sonnet" in TARGET_CLAUDE_SONNET_MODEL
