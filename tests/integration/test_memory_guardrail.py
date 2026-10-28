"""Integration tests for AgentCore Memory + Bedrock Guardrail (task 14.3).

The live AgentCore Memory and Bedrock Guardrail services cannot be exercised
in this environment, so — consistent with the other integration tests
(``tests/integration/test_gateway_auth_policy.py``) — this module combines two
complementary styles:

1. **Behavioral tests of the agent memory hooks** (``agent/memory_hooks.py``)
   driven against a *fake, in-memory* ``MemoryClientProtocol`` implementation.
   The real ``bedrock_agentcore.memory.MemoryClient`` satisfies the same
   structural protocol; swapping in a faithful in-memory double lets us assert
   the hooks' read/write semantics (how turns and preferences are keyed by
   ``actorId``/``sessionId``) exactly as they will run against the live
   service, with no AWS calls.

2. **CDK synthesis/assertion checks of the agent stack**
   (``stacks/agent_stack.py``). We synthesize a real ``AgentStack`` (wired with
   a ``FoundationStack`` for the shared KMS key / permissions boundary and
   representative Cognito issuer / app-client, exactly as ``app.py`` wires it)
   and use ``aws_cdk.assertions.Template`` to assert the managed-service
   configuration that *causes* the runtime behavior: a bounded short-term
   event expiry, the user-preference strategy/namespace, and a blocking
   guardrail with safe-refusal messaging.

Acceptance criteria covered:

* **Requirement 8.1 — session turns persist and retrieve (short-term).**
  Against the fake client, ``record_turn`` appends session events keyed by
  ``actor + session`` and ``load_session_context`` returns those turns. The
  stack declares a bounded ``EventExpiryDuration`` so short-term memory has a
  finite retention.

* **Requirements 8.2 / 8.3 — a user preference persists across sessions.**
  ``save_preference`` writes to the per-actor user-preference namespace
  (``/preferences/{actorId}``). A *new* hooks instance for the **same actor**
  but a **different session_id** retrieves that preference via
  ``load_session_context`` — proving cross-session persistence is keyed by
  identity, not by session. The stack declares the ``user_preference``
  strategy with that exact namespace template.

* **Requirement 9.2 — the Guardrail blocks a known-violating prompt.**
  The live block is enforced by the deployed Bedrock Guardrail, so we assert
  the configuration that produces a block + safe refusal: content filters
  present with a blocking strength (>= MEDIUM) and non-empty
  ``blocked_input/outputs`` messaging as the safe refusal returned to the
  caller.

Why a fake memory client (rather than a mock)
---------------------------------------------
The fake is a *faithful* in-memory implementation of the four methods the
hooks use (``create_event``, ``list_events``, ``retrieve_memories``,
``create_memory_record``). It stores events under an ``(actor, session)`` key
and long-term records under a namespace key — mirroring the AgentCore Memory
organization (short-term keyed by ``actorId + sessionId``; long-term records
scoped per-actor namespace). This exercises the hooks' real logic end to end
without stubbing out the behavior under test.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Sequence, Tuple

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from agent.config import AgentConfig
from agent.memory_hooks import (
    ROLE_ASSISTANT,
    ROLE_USER,
    PharmaPulseMemoryHooks,
)
from stacks.config import PRIMARY_REGION
from stacks.foundation_stack import FoundationStack
from stacks.agent_stack import AgentStack

# Mark the whole module as an integration test (marker configured in
# pyproject.toml). It combines behavioral hook tests against a fake memory
# client with CDK synthesis/assertion checks of the agent stack.
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fake in-memory AgentCore Memory client (satisfies MemoryClientProtocol).
# ---------------------------------------------------------------------------
class FakeMemoryClient:
    """A faithful in-memory double for the AgentCore Memory client.

    Mirrors the service's data organization:

    * **Short-term** events are an append-only log keyed by
      ``(memory_id, actor_id, session_id)`` — exactly how AgentCore scopes
      short-term memory (``actorId + sessionId``).
    * **Long-term** records are stored per ``(memory_id, namespace)``. Because
      the user-preference namespace is ``/preferences/{actorId}``, records for
      an actor live under the same key regardless of session, which is what
      makes a preference visible across sessions.

    Only the four methods the hooks depend on are implemented; the structural
    ``MemoryClientProtocol`` is satisfied so this can be injected directly.
    """

    def __init__(self) -> None:
        # (memory_id, actor_id, session_id) -> list of {"text", "role"} turns.
        self.events: Dict[Tuple[str, str, str], List[Dict[str, str]]] = (
            defaultdict(list)
        )
        # (memory_id, namespace) -> list of record content strings.
        self.records: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        # Call counters for optional assertions.
        self.create_event_calls = 0
        self.create_record_calls = 0

    def create_event(
        self,
        *,
        memory_id: str,
        actor_id: str,
        session_id: str,
        messages: Sequence[Tuple[str, str]],
    ) -> Dict[str, Any]:
        self.create_event_calls += 1
        key = (memory_id, actor_id, session_id)
        for text, role in messages:
            self.events[key].append({"text": text, "role": role})
        return {"eventId": f"evt-{len(self.events[key])}"}

    def list_events(
        self,
        *,
        memory_id: str,
        actor_id: str,
        session_id: str,
        max_results: int = 20,
    ) -> List[Dict[str, str]]:
        key = (memory_id, actor_id, session_id)
        # Return the most recent ``max_results`` events, newest-inclusive.
        return list(self.events[key])[-max_results:]

    def retrieve_memories(
        self,
        *,
        memory_id: str,
        namespace: str,
        query: str,
        top_k: int = 5,
    ) -> List[Dict[str, str]]:
        key = (memory_id, namespace)
        # A real service does semantic ranking; the fake returns the stored
        # records (most-recent first) up to ``top_k``. Keying by namespace is
        # the property under test, so no ranking is needed here.
        contents = list(reversed(self.records[key]))[:top_k]
        return [{"content": c} for c in contents]

    def create_memory_record(
        self,
        *,
        memory_id: str,
        namespace: str,
        content: str,
    ) -> Dict[str, Any]:
        self.create_record_calls += 1
        key = (memory_id, namespace)
        self.records[key].append(content)
        return {"memoryRecordId": f"rec-{len(self.records[key])}"}


# ---------------------------------------------------------------------------
# Shared config / constants.
# ---------------------------------------------------------------------------
_MEMORY_ID = "mem-abc123"
_ACTOR_ID = "cognito-sub-0001"  # the caller's Cognito ``sub`` (identity)
_SESSION_A = "session-A"
_SESSION_B = "session-B"  # a different session for the same actor


def _config() -> AgentConfig:
    """An AgentConfig with a memory id set so the hooks are *enabled*.

    ``memory_id`` must be truthy for the hooks to actually touch memory; the
    default user-preference namespace (``/preferences/{actorId}``) is used so
    the namespace keying matches the deployed stack.
    """
    return AgentConfig(memory_id=_MEMORY_ID)


def _hooks(client: FakeMemoryClient, session_id: str) -> PharmaPulseMemoryHooks:
    """Build a hooks instance bound to the fake client for a given session."""
    return PharmaPulseMemoryHooks(
        config=_config(),
        actor_id=_ACTOR_ID,
        session_id=session_id,
        memory_client=client,
    )


# ===========================================================================
# (a) Behavioral tests: memory hooks against the fake in-memory client.
# ===========================================================================
class TestSessionTurnsPersistAndRetrieve:
    """Requirement 8.1 — short-term session turns persist and retrieve."""

    def test_hooks_are_enabled_with_memory_id_and_client(self):
        """Sanity: with a memory id + client the hooks actually touch memory
        (otherwise they are intentional no-ops and nothing would persist)."""
        client = FakeMemoryClient()
        hooks = _hooks(client, _SESSION_A)
        assert hooks.enabled is True

    def test_recorded_turns_are_retrieved_in_same_session(self):
        """record_turn writes session events keyed by actor+session, and
        load_session_context returns exactly those turns (Req 8.1)."""
        client = FakeMemoryClient()
        hooks = _hooks(client, _SESSION_A)

        assert hooks.record_turn("Is amoxicillin in shortage?", ROLE_USER)
        assert hooks.record_turn(
            "Amoxicillin has an active shortage.", ROLE_ASSISTANT
        )

        context = hooks.load_session_context()
        turns = context["recent_turns"]

        assert len(turns) == 2
        texts = [t["text"] for t in turns]
        roles = [t["role"] for t in turns]
        assert texts == [
            "Is amoxicillin in shortage?",
            "Amoxicillin has an active shortage.",
        ]
        assert roles == [ROLE_USER, ROLE_ASSISTANT]

    def test_turns_are_keyed_by_session_not_leaked_across_sessions(self):
        """Short-term memory is scoped to actor+session: a turn written in
        session A must NOT appear when loading session B (Req 8.1)."""
        client = FakeMemoryClient()
        hooks_a = _hooks(client, _SESSION_A)
        hooks_a.record_turn("Session A only", ROLE_USER)

        hooks_b = _hooks(client, _SESSION_B)
        context_b = hooks_b.load_session_context()

        assert context_b["recent_turns"] == []

    def test_empty_turn_is_not_persisted(self):
        """An empty message is not written (guards against junk events)."""
        client = FakeMemoryClient()
        hooks = _hooks(client, _SESSION_A)
        assert hooks.record_turn("", ROLE_USER) is False
        assert hooks.load_session_context()["recent_turns"] == []


class TestUserPreferencePersistsAcrossSessions:
    """Requirements 8.2 / 8.3 — a preference persists across sessions."""

    def test_preference_saved_in_one_session_is_read_in_a_new_session(self):
        """save_preference writes to the per-actor user-preference namespace;
        a NEW hooks instance for the SAME actor but a DIFFERENT session_id
        retrieves it via load_session_context — proving cross-session
        persistence keyed by identity (Req 8.2, 8.3)."""
        client = FakeMemoryClient()

        # Session A: the user states a preference; the agent persists it.
        hooks_a = _hooks(client, _SESSION_A)
        preference = "Preferred pharmacy location is Riverside (LOC-02)."
        assert hooks_a.save_preference(preference) is True

        # Session B: a brand-new hooks instance, same actor, different
        # session. On session start it loads the stored preference.
        hooks_b = _hooks(client, _SESSION_B)
        assert hooks_b.session_id != hooks_a.session_id
        assert hooks_b.actor_id == hooks_a.actor_id

        context_b = hooks_b.load_session_context()
        pref_texts = [p["content"] for p in context_b["preferences"]]
        assert preference in pref_texts

    def test_preference_written_to_per_actor_namespace(self):
        """The preference record is written to ``/preferences/{actorId}`` so
        it is isolated per user and discoverable by identity (Req 8.2)."""
        client = FakeMemoryClient()
        hooks = _hooks(client, _SESSION_A)
        hooks.save_preference("Preferred supplier is Acme Pharma.")

        expected_namespace = f"/preferences/{_ACTOR_ID}"
        assert (_MEMORY_ID, expected_namespace) in client.records
        assert client.records[(_MEMORY_ID, expected_namespace)] == [
            "Preferred supplier is Acme Pharma."
        ]

    def test_preference_isolated_from_a_different_actor(self):
        """A different actor's new session must NOT see this user's stored
        preference, because namespaces are per-actor (Req 8.2)."""
        client = FakeMemoryClient()
        _hooks(client, _SESSION_A).save_preference("Prefers LOC-02")

        other = PharmaPulseMemoryHooks(
            config=_config(),
            actor_id="cognito-sub-9999",  # a different identity
            session_id="session-X",
            memory_client=client,
        )
        context = other.load_session_context()
        assert context["preferences"] == []


# ===========================================================================
# (b) CDK synthesis/assertion checks of the agent stack.
# ===========================================================================
# Representative inputs mirroring how ``app.py`` wires the AgentStack.
_ACCOUNT = "123456789012"
_USER_POOL_ISSUER = (
    "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_ABC123xyz"
)
_APP_CLIENT_ID = "0123456789abcdefghijklmnop"
_GATEWAY_URL = "https://gateway.pharmapulse.example.com/mcp"

# Resource type names (confirmed against the vendored aws-cdk-lib L1s).
_T_MEMORY = "AWS::BedrockAgentCore::Memory"
_T_GUARDRAIL = "AWS::Bedrock::Guardrail"

# Content-filter strengths that count as "blocking" (>= MEDIUM). NONE/LOW are
# treated as non-blocking for the purposes of Req 9.2.
_BLOCKING_STRENGTHS = {"MEDIUM", "HIGH"}


@pytest.fixture(scope="module")
def synthesized():
    """Synthesize an AgentStack (plus its FoundationStack) once per module.

    The AgentStack is constructed with the foundation's shared KMS key +
    permissions boundary and representative Cognito issuer / app-client /
    Gateway URL, exactly as ``app.py`` wires them, in the primary region
    (us-east-2). Synthesis inspects the CloudFormation only — no AWS calls.
    """
    app = cdk.App()
    env = cdk.Environment(account=_ACCOUNT, region=PRIMARY_REGION)
    foundation = FoundationStack(app, "TestFoundation", env=env)
    agent = AgentStack(
        app,
        "TestAgent",
        gateway_url=_GATEWAY_URL,
        user_pool_issuer=_USER_POOL_ISSUER,
        app_client_id=_APP_CLIENT_ID,
        encryption_key=foundation.encryption_key,
        permissions_boundary=foundation.permissions_boundary,
        env=env,
    )
    return agent, Template.from_stack(agent)


class TestMemoryShortTermRetention:
    """Requirement 8.1 — short-term memory has a bounded event expiry."""

    def test_single_memory_resource_exists(self, synthesized):
        _, template = synthesized
        template.resource_count_is(_T_MEMORY, 1)

    def test_short_term_event_expiry_is_bounded(self, synthesized):
        """A finite ``EventExpiryDuration`` means short-term session events
        are retained for a bounded window (Req 8.1), not indefinitely and not
        zero. We assert it is present and a positive integer."""
        _, template = synthesized
        memories = template.find_resources(_T_MEMORY)
        assert len(memories) == 1
        props = next(iter(memories.values()))["Properties"]
        expiry = props.get("EventExpiryDuration")
        assert expiry is not None, "short-term memory must declare an expiry"
        assert isinstance(expiry, (int, float)) and expiry > 0


class TestMemoryUserPreferenceStrategy:
    """Requirements 8.2 / 8.3 — user-preference strategy + per-actor namespace."""

    def test_user_preference_strategy_declares_per_actor_namespace(
        self, synthesized
    ):
        """The Memory resource declares a user_preference strategy whose
        namespace template is ``/preferences/{actorId}`` — the same namespace
        the hooks write to and read from, which is what makes a preference
        persist across sessions for a given identity (Req 8.2, 8.3)."""
        _, template = synthesized
        template.has_resource_properties(
            _T_MEMORY,
            {
                "MemoryStrategies": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "UserPreferenceMemoryStrategy": Match.object_like(
                                    {
                                        "NamespaceTemplates": [
                                            "/preferences/{actorId}"
                                        ]
                                    }
                                )
                            }
                        )
                    ]
                )
            },
        )

    def test_namespace_matches_agent_config_default(self, synthesized):
        """The stack namespace must match the agent's configured namespace so
        the hooks and the deployed strategy agree on where preferences live."""
        from agent.config import DEFAULT_USER_PREFERENCE_NAMESPACE

        _, template = synthesized
        props = next(iter(template.find_resources(_T_MEMORY).values()))[
            "Properties"
        ]
        strategies = props["MemoryStrategies"]
        namespaces = [
            ns
            for s in strategies
            if "UserPreferenceMemoryStrategy" in s
            for ns in s["UserPreferenceMemoryStrategy"]["NamespaceTemplates"]
        ]
        assert DEFAULT_USER_PREFERENCE_NAMESPACE in namespaces


class TestGuardrailBlocksViolatingPrompt:
    """Requirement 9.2 — the Guardrail blocks a known-violating prompt.

    The live block is enforced by the deployed Bedrock Guardrail (the model
    can't run here), so we assert the configuration that *causes* a violating
    prompt to be blocked and a safe refusal to be returned.
    """

    def test_single_guardrail_exists(self, synthesized):
        _, template = synthesized
        template.resource_count_is(_T_GUARDRAIL, 1)

    def test_guardrail_has_blocking_content_filters(self, synthesized):
        """Content filters must be present with a *blocking* strength
        (>= MEDIUM) on input; a prompt that trips any such filter is blocked
        rather than passed through (Req 9.2)."""
        _, template = synthesized
        props = next(iter(template.find_resources(_T_GUARDRAIL).values()))[
            "Properties"
        ]
        filters = props["ContentPolicyConfig"]["FiltersConfig"]
        assert filters, "guardrail must declare content filters"

        # At least one filter blocks on input at MEDIUM or stronger.
        blocking = [
            f
            for f in filters
            if f.get("InputStrength") in _BLOCKING_STRENGTHS
        ]
        assert blocking, (
            "expected at least one content filter at >= MEDIUM input strength "
            "so a violating prompt is blocked"
        )

        # Every declared filter blocks at >= MEDIUM on at least one direction
        # (input or output); none is effectively disabled on both.
        for f in filters:
            strengths = {f.get("InputStrength"), f.get("OutputStrength")}
            assert strengths & _BLOCKING_STRENGTHS, (
                f"filter {f.get('Type')} is not blocking on either direction"
            )

    def test_known_harmful_categories_are_filtered(self, synthesized):
        """The standard harmful-content categories are covered, so a known
        violating prompt (e.g. hateful/violent content) trips a filter."""
        _, template = synthesized
        props = next(iter(template.find_resources(_T_GUARDRAIL).values()))[
            "Properties"
        ]
        types = {
            f["Type"]
            for f in props["ContentPolicyConfig"]["FiltersConfig"]
        }
        # These categories make a "known-violating" prompt detectable.
        for required in ("HATE", "VIOLENCE", "SEXUAL", "MISCONDUCT"):
            assert required in types, f"missing content filter: {required}"

    def test_blocked_input_messaging_is_the_safe_refusal(self, synthesized):
        """When input is blocked, the Guardrail returns a non-empty safe
        refusal message to the caller (Req 9.2)."""
        _, template = synthesized
        props = next(iter(template.find_resources(_T_GUARDRAIL).values()))[
            "Properties"
        ]
        refusal = props.get("BlockedInputMessaging")
        assert isinstance(refusal, str) and refusal.strip(), (
            "blocked input must return a non-empty safe refusal message"
        )

    def test_blocked_output_messaging_is_the_safe_refusal(self, synthesized):
        """When model output is blocked, a non-empty safe refusal is returned
        instead of the violating content (Req 9.2)."""
        _, template = synthesized
        props = next(iter(template.find_resources(_T_GUARDRAIL).values()))[
            "Properties"
        ]
        refusal = props.get("BlockedOutputsMessaging")
        assert isinstance(refusal, str) and refusal.strip(), (
            "blocked output must return a non-empty safe refusal message"
        )

    def test_guardrail_is_wired_to_agent_runtime_env(self, synthesized):
        """The agent runtime receives the guardrail id + version via its
        environment, so the deployed agent actually applies this guardrail to
        every model interaction — the live block path for Req 9.2.

        The env values are CloudFormation references (GetAtt) to the guardrail
        resource, so we assert the keys are present rather than literals.
        """
        _, template = synthesized
        runtimes = template.find_resources("AWS::BedrockAgentCore::Runtime")
        assert len(runtimes) == 1
        props = next(iter(runtimes.values()))["Properties"]
        env = props.get("EnvironmentVariables", {})
        assert "BEDROCK_GUARDRAIL_ID" in env
        assert "BEDROCK_GUARDRAIL_VERSION" in env


class TestRegionPlacement:
    """Requirement 17.6 — the agent is a primary-region stack (us-east-2)."""

    def test_stack_region_is_primary(self, synthesized):
        stack, _ = synthesized
        assert stack.region == PRIMARY_REGION == "us-east-2"
