"""AgentCore Memory hooks for the Strands agent (Requirements 8.1, 8.2, 8.3).

The agent uses AgentCore Memory through the Strands **hook** system so memory
reads/writes attach to the agent lifecycle without cluttering the main loop:

* **Short-term session memory (Req 8.1).** Every conversation turn (user and
  assistant message) is appended to AgentCore short-term memory keyed by
  ``actorId + sessionId`` via ``create_event``. On session start the recent
  turns for the session are loaded back so context is retained within the
  active session.

* **Long-term user-preference memory (Req 8.2, 8.3).** On session start the
  caller's stored preferences (preferred pharmacy location, preferred
  suppliers) are retrieved from the per-actor user-preference namespace
  (``/preferences/{actorId}``) and injected into context so a returning user
  does not have to repeat them. When the user states a new preference it is
  written back to that same namespace, keyed by identity.

Implementation strategy
------------------------
This module exposes :class:`PharmaPulseMemoryHooks`, a Strands
``HookProvider``. The Strands ``AgentCoreMemorySessionManager`` /
``bedrock_agentcore.memory`` client is the live integration point; both SDKs
are imported lazily so this module imports cleanly without them. A small
:class:`MemoryClientProtocol` describes the client surface the hooks use
(``create_event``, ``list_events``, ``retrieve_memories``,
``create_memory_record``), which lets the hooks be unit-tested against a fake
in-memory client and lets :func:`build_memory_client` supply the real
AgentCore Memory client when running on Runtime.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from agent.config import AgentConfig

# Roles used when tagging turns written to short-term memory.
ROLE_USER = "USER"
ROLE_ASSISTANT = "ASSISTANT"


class MemoryClientProtocol(Protocol):
    """The minimal AgentCore Memory client surface the hooks depend on.

    The real ``bedrock_agentcore.memory.MemoryClient`` satisfies this
    structurally; a fake implementing these methods is used in tests.
    """

    def create_event(
        self,
        *,
        memory_id: str,
        actor_id: str,
        session_id: str,
        messages: Sequence[Tuple[str, str]],
    ) -> Any:
        """Append one or more ``(text, role)`` messages as a session event."""
        ...

    def list_events(
        self,
        *,
        memory_id: str,
        actor_id: str,
        session_id: str,
        max_results: int = 20,
    ) -> Any:
        """List recent short-term events for a session."""

    def retrieve_memories(
        self,
        *,
        memory_id: str,
        namespace: str,
        query: str,
        top_k: int = 5,
    ) -> Any:
        """Retrieve long-term records from a namespace by semantic query."""

    def create_memory_record(
        self,
        *,
        memory_id: str,
        namespace: str,
        content: str,
    ) -> Any:
        """Write a long-term record into a namespace."""


class PharmaPulseMemoryHooks:
    """Strands ``HookProvider`` wiring AgentCore Memory into the agent.

    The hooks are keyed by ``actor_id`` (the caller's Cognito ``sub`` /
    identity) and ``session_id`` (per chat session) so short-term memory is
    scoped to the active session and long-term preferences are isolated per
    user (Requirements 8.1–8.3). The provider degrades gracefully: if no memory
    client is available (``memory_id`` unset or SDK missing) the hooks become
    no-ops rather than failing the turn, so the agent still answers.
    """

    def __init__(
        self,
        config: AgentConfig,
        actor_id: str,
        session_id: str,
        memory_client: Optional[MemoryClientProtocol] = None,
    ) -> None:
        self.config = config
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_id = config.memory_id
        self.namespace = config.namespace_for(actor_id)
        # When no client is supplied, attempt to build the live one; stay a
        # no-op if that is not possible (missing SDK / memory id).
        self._client = memory_client or build_memory_client(config)
        self._enabled = bool(self._client and self.memory_id)

    # -- Public, SDK-independent operations ------------------------------
    # These are written so they can be invoked directly by tests and by the
    # lifecycle-hook callbacks below.

    @property
    def enabled(self) -> bool:
        """Whether memory operations will actually touch AgentCore Memory."""
        return self._enabled

    def load_session_context(self) -> Dict[str, Any]:
        """Load short-term turns + long-term preferences for the session.

        Called on agent initialization. Returns a dict with ``recent_turns``
        (short-term, Req 8.1) and ``preferences`` (long-term user-preference
        namespace, Req 8.3). On any failure returns empty collections so the
        session still starts.
        """
        if not self._enabled:
            return {"recent_turns": [], "preferences": []}

        recent_turns: List[Any] = []
        preferences: List[Any] = []
        try:
            recent_turns = _coerce_list(
                self._client.list_events(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    max_results=self.config.session_turn_limit,
                )
            )
        except Exception:  # pragma: no cover - defensive; live-service errors
            recent_turns = []

        try:
            preferences = _coerce_list(
                self._client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=self.namespace,
                    query="preferred pharmacy location and preferred suppliers",
                    top_k=self.config.preference_top_k,
                )
            )
        except Exception:  # pragma: no cover - defensive
            preferences = []

        return {"recent_turns": recent_turns, "preferences": preferences}

    def record_turn(self, text: str, role: str) -> bool:
        """Append a single conversation turn to short-term memory (Req 8.1).

        Returns ``True`` if the event was written, ``False`` if memory is
        disabled or the write failed (failures are swallowed so a memory
        outage never breaks the conversation).
        """
        if not self._enabled or not text:
            return False
        try:
            self._client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[(text, role)],
            )
            return True
        except Exception:  # pragma: no cover - defensive
            return False

    def save_preference(self, preference_text: str) -> bool:
        """Persist a stated user preference to the per-actor namespace (Req 8.2).

        The record is written to ``/preferences/{actorId}`` so it is retrieved
        for this user on their next session (Req 8.3). Returns ``True`` on a
        successful write.
        """
        if not self._enabled or not preference_text:
            return False
        try:
            self._client.create_memory_record(
                memory_id=self.memory_id,
                namespace=self.namespace,
                content=preference_text,
            )
            return True
        except Exception:  # pragma: no cover - defensive
            return False

    # -- Strands hook registration ---------------------------------------
    def register_hooks(self, registry: Any, **_: Any) -> None:
        """Register lifecycle callbacks with a Strands ``HookRegistry``.

        Implements the Strands ``HookProvider`` protocol. The Strands hook
        event classes are imported lazily; if they are unavailable this is a
        no-op so the provider can still be constructed and its public methods
        used (e.g. in tests) without the SDK.

        Wiring:
        * ``AgentInitializedEvent`` -> :meth:`load_session_context` (preload
          session turns + preferences).
        * ``MessageAddedEvent``     -> :meth:`record_turn` (persist each turn
          to short-term memory).
        """
        try:
            from strands.hooks import (  # type: ignore
                AgentInitializedEvent,
                MessageAddedEvent,
            )
        except Exception:  # pragma: no cover - exercised only w/o SDK
            return

        registry.add_callback(AgentInitializedEvent, self._on_agent_initialized)
        registry.add_callback(MessageAddedEvent, self._on_message_added)

    # -- Hook callbacks ---------------------------------------------------
    def _on_agent_initialized(self, event: Any) -> None:
        """Preload short-term turns and long-term preferences into context."""
        context = self.load_session_context()
        agent = getattr(event, "agent", None)
        if agent is None:
            return
        preview = _format_context_preview(context)
        if not preview:
            return
        # Append a system-context note so the model sees prior turns and the
        # user's stored preferences at session start (Req 8.1, 8.3).
        try:
            messages = getattr(agent, "messages", None)
            if isinstance(messages, list):
                messages.append(
                    {
                        "role": "user",
                        "content": [{"text": preview}],
                    }
                )
        except Exception:  # pragma: no cover - defensive
            return

    def _on_message_added(self, event: Any) -> None:
        """Persist the just-added message to short-term memory (Req 8.1)."""
        message = getattr(event, "message", None)
        text, role = _extract_message(message)
        if text:
            self.record_turn(text, role)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _coerce_list(value: Any) -> List[Any]:
    """Normalize a memory-client return value into a list.

    AgentCore memory responses vary (list, or a dict with an ``events`` /
    ``memoryRecords`` / ``memoryRecordSummaries`` key); this flattens the
    common shapes into a plain list and tolerates ``None``.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("events", "memoryRecords", "memoryRecordSummaries", "records", "items"):
            inner = value.get(key)
            if isinstance(inner, list):
                return inner
        return [value]
    return [value]


def _extract_message(message: Any) -> Tuple[str, str]:
    """Extract ``(text, role)`` from a Strands message dict.

    Returns an empty text when the message cannot be interpreted so the caller
    skips the write.
    """
    if not isinstance(message, dict):
        return "", ROLE_USER
    raw_role = str(message.get("role", "user")).lower()
    role = ROLE_ASSISTANT if raw_role.startswith("assist") else ROLE_USER
    content = message.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        text = "\n".join(parts)
    return text.strip(), role


def _format_context_preview(context: Dict[str, Any]) -> str:
    """Render retrieved preferences/turns into a short context note."""
    prefs = context.get("preferences") or []
    lines: List[str] = []
    pref_texts = [_record_text(p) for p in prefs]
    pref_texts = [t for t in pref_texts if t]
    if pref_texts:
        lines.append("Known preferences for this user:")
        lines.extend(f"- {t}" for t in pref_texts)
    if lines:
        return "\n".join(lines)
    return ""


def _record_text(record: Any) -> str:
    """Pull the human-readable text out of a long-term memory record."""
    if isinstance(record, str):
        return record.strip()
    if isinstance(record, dict):
        for key in ("content", "text", "memoryContent", "value"):
            val = record.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, dict) and isinstance(val.get("text"), str):
                return val["text"].strip()
    return ""


def build_memory_client(config: AgentConfig) -> Optional[MemoryClientProtocol]:
    """Construct the live AgentCore Memory client, or ``None`` if unavailable.

    Returns ``None`` (so the hooks become no-ops) when the ``bedrock_agentcore``
    SDK is not installed or no ``memory_id`` is configured — this is what keeps
    the module importable and the agent runnable without live AWS/memory.
    """
    if not config.memory_id:
        return None
    try:
        from bedrock_agentcore.memory import MemoryClient  # type: ignore
    except Exception:  # pragma: no cover - exercised only w/o SDK
        return None
    try:
        return MemoryClient(region_name=config.region)  # type: ignore[no-any-return]
    except Exception:  # pragma: no cover - defensive (no creds, etc.)
        return None


__all__ = [
    "ROLE_USER",
    "ROLE_ASSISTANT",
    "MemoryClientProtocol",
    "PharmaPulseMemoryHooks",
    "build_memory_client",
]
