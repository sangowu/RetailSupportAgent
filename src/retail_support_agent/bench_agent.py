"""Plug this project's guard into τ²-bench so the in-house implementation is the
subject under test.

## Why this module exists

M0's 0.8509 did not measure this project. The benchmark ran upstream's own
`llm_agent` (`agent="llm_agent"` in `build_run_config()`), while this project's actual
claims — two-stage confirmation for writes, confirmation bound to specific arguments,
order ownership checks — all live in `guardrails.py` and were exercised only by
demo_flows and unit tests. Upstream's agent does not contain the word `confirm`.

This module registers `rsa_guarded`, putting the guard in front of 114 real tasks.

## Subclass, do not rewrite

The system prompt, message handling and LLM calls all come from upstream `LLMAgent`;
only `generate_next_message` is overridden. That makes the comparison against
`baseline-v2` a comparison of "guard or no guard" rather than of two different
implementations, which would show nothing.

## One difference from the M0.3 application path

The application path has a fixed simulated identity (`SessionState.user_id` is
given). The benchmark does not: the agent must complete identification per upstream
policy before it knows who it is serving. Identity here is therefore learned from the
conversation, by reading what `find_user_id_by_*` returned. Every write is denied
until an identity exists.

Order ownership likewise uses only what the conversation has already looked up. No
extra tool call is made: an extra call would produce side effects outside the score,
the cost and the action metrics, making the run's numbers unexplainable. When
ownership is unknown the guard asks for the lookup first, which the policy requires
anyway.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ConfigDict

from retail_support_agent.domain import WRITE_TOOLS
from retail_support_agent.guardrails import (
    Decision,
    GuardVerdict,
    ProposedCall,
    SessionLimits,
    SessionState,
)

AGENT_NAME = "rsa_guarded"

# These two tools return the user id; they are the identification entry point in the
# upstream retail policy.
IDENTITY_TOOLS = frozenset({"find_user_id_by_email", "find_user_id_by_name_zip"})

# Order ownership is read from this tool's result.
ORDER_LOOKUP_TOOL = "get_order_details"

NO_IDENTITY_REASON = (
    "The customer has not been authenticated yet. Refusing to run a write action. "
    "Call find_user_id_by_email or find_user_id_by_name_zip first."
)
UNKNOWN_OWNER_REASON = (
    "Order {order_id} has not been looked up, so its owner is unknown. "
    "Refusing to run a write action. Call get_order_details on it first."
)


def _clean(raw: Any) -> str:
    """An upstream tool result may be a pydantic model or already a string."""
    if isinstance(raw, str):
        return raw
    dump = getattr(raw, "model_dump_json", None)
    if callable(dump):
        return str(dump())
    return json.dumps(raw, ensure_ascii=False, default=str)


class GuardState:
    """Guard state for one task.

    Must not be module-level global state: batch runs are concurrent, and a global
    would let tasks contaminate each other. The symptom would be a few tasks passing
    confirmation for no visible reason, which raises no error.
    """

    def __init__(self) -> None:
        self.identity: str | None = None
        self.order_owner: dict[str, str] = {}
        # A ToolMessage carries an id but not the tool name, so the id -> name mapping
        # is tracked here. Otherwise an arriving result cannot be classified as an
        # identity lookup or an order lookup.
        self.call_names: dict[str, str] = {}
        self.confirmed: set[str] = set()
        self.pending: ProposedCall | None = None
        # Record of what the guard blocked in this task, used to classify regressions.
        self.interventions: list[dict[str, str]] = []

    def as_session(self) -> SessionState:
        """Borrow the application-side SessionState to reuse WriteGuard's logic.

        Callers must block before reaching here when no identity exists:
        SessionState.user_id is a required str, and a placeholder value would be
        compared by the ownership check and produce a misleading verdict.
        """
        assert self.identity is not None
        state = SessionState(user_id=self.identity, limits=SessionLimits())
        state.confirmed_fingerprints = set(self.confirmed)
        return state


def observe_tool_result(guard: GuardState, name: str, content: str) -> None:
    """Update identity and order ownership from a tool result.

    A parse failure teaches nothing and is never guessed at: a wrong guess would let
    through a write that should have been blocked.
    """
    if name in IDENTITY_TOOLS:
        candidate = content.strip().strip('"')
        # A user id looks like ava_moore_2033; a failed lookup returns prose.
        if candidate and " " not in candidate and "\n" not in candidate:
            guard.identity = candidate
        return

    if name == ORDER_LOOKUP_TOOL:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        order_id = payload.get("order_id")
        user_id = payload.get("user_id")
        if isinstance(order_id, str) and isinstance(user_id, str):
            guard.order_owner[order_id] = user_id


def review_call(guard: GuardState, known_tools: set[str], call: ProposedCall) -> GuardVerdict:
    """Judge one proposed tool call.

    Reads pass. Writes go through three checks in order: identity established,
    ownership known and matching, confirmation present.

    Ownership comes only from what the conversation already looked up
    (`guard.order_owner`), and that mapping must be handed to `WriteGuard`
    explicitly: it would otherwise call `owner_lookup` itself, and no extra tool
    calls are made in the benchmark (see the module docstring). An earlier version
    passed a lookup that always returned None, so every write was judged "order does
    not exist" — apparently safe, in fact blocking every write for an unexplainable
    reason.
    """
    from retail_support_agent.guardrails import WriteGuard

    guard_impl = WriteGuard(known_tools, guard.order_owner.get)

    if call.name not in WRITE_TOOLS:
        read_verdict: GuardVerdict = guard_impl.review(call, _read_only_session(guard))
        return read_verdict

    if guard.identity is None:
        return GuardVerdict(Decision.DENIED_OWNERSHIP, NO_IDENTITY_REASON, call)

    order_id = call.arguments.get("order_id")
    if isinstance(order_id, str) and order_id not in guard.order_owner:
        return GuardVerdict(
            Decision.DENIED_OWNERSHIP, UNKNOWN_OWNER_REASON.format(order_id=order_id), call
        )

    verdict: GuardVerdict = guard_impl.review(call, guard.as_session())
    return verdict


def _read_only_session(guard: GuardState) -> SessionState:
    """Reads do not need an identity, but WriteGuard's signature requires a SessionState."""
    return SessionState(user_id=guard.identity or "", limits=SessionLimits())


# A negation that points at an action counts anywhere in the reply.
# Bare "don't" / "do not" used to veto the whole message, and that cost task 80: the
# user wrote "my Spanish is decent but I don't think that's Spanish! ... YES — go
# ahead!" and a sentence about language cancelled an unambiguous agreement in the same
# message. A negation now counts only when an action verb follows within a few words.
_NEGATED_ACTION = re.compile(
    r"\b(?:don't|dont|do not|never)\b[^.,;!?\n]{0,24}"
    r"\b(?:do|cancel|change|modify|update|return|exchange|place|proceed|submit|confirm|go)\b"
)
# These phrases are refusals on their own and need no verb.
HARD_NEGATIVES = ("cancel that", "not yet", "hold off")
# Bare "no" / "not" counts only in the first clause; see is_affirmative.
LEADING_NEGATIVES = ("no", "nope", "wait", "not")
POSITIVES = (
    "yes",
    "yep",
    "confirm",
    "go ahead",
    "please do",
    "correct",
    "ok",
    "okay",
    "sure",
)

_CLAUSE_BREAK = re.compile(r"[,;.!?\n—–:]")  # noqa: RUF001 - dashes are clause breaks on purpose


def is_affirmative(text: str) -> bool:
    """Whether the user explicitly agreed.

    Bare negations count only in the first clause. This is not leniency; it came out
    of a real infinite loop. The simulated user replied

        Confirmed — cancel #W5199551, "no longer needed."

    which is unambiguous agreement, but the quoted cancellation reason contains "no
    longer". Substring matching on "no" read it as a refusal, the confirmation never
    arrived, the model resent the call, the guard blocked it again, and one task
    looped 93 times on the same step until it hit the step limit and scored 0.

    The rule has three layers:
      - a negation pointing at an action ("don't cancel", "do not proceed") counts anywhere
      - explicit refusal phrases (cancel that, not yet, hold off) also count anywhere
      - bare no / not counts only in the first clause, so quoted text is not a stance

    The first layer used to be a bare "don't", which failed on task 80: the user used
    "don't" in a sentence about language and it cancelled "YES — go ahead!" in the same
    message; the exchange never ran and the task scored 0. The bias remains toward
    reading a refusal: a false refusal costs one extra round trip, while a false
    agreement is an unconfirmed write.

    Kept separate from the application-side `agent._is_affirmative` so the two word
    lists do not constrain each other.
    """
    lowered = text.strip().lower()
    if not lowered:
        return False
    if _NEGATED_ACTION.search(lowered):
        return False
    if any(word in lowered for word in HARD_NEGATIVES):
        return False

    head = _CLAUSE_BREAK.split(lowered, maxsplit=1)[0]
    head_words = set(re.findall(r"[a-z']+", head)) | set(head)
    if any(word in head_words for word in LEADING_NEGATIVES):
        return False

    return any(word in lowered for word in POSITIVES)


def build_agent_class() -> Any:
    """Build the agent class lazily.

    Upstream reads `TAU2_DATA_DIR` at import time, so `import tau2` cannot happen at
    module level: that would force any caller who only wants `guardrails` to prepare a
    data directory first.
    """
    from tau2.agent.llm_agent import LLMAgent, LLMAgentState
    from tau2.data_model.message import (
        AssistantMessage,
        MultiToolMessage,
        ToolMessage,
        UserMessage,
    )

    class GuardedState(LLMAgentState):  # type: ignore[misc]
        """Upstream state plus this project's guard state.

        The guard state hangs off the state object rather than the agent instance:
        batch runs are concurrent, and instance attributes would let tasks contaminate
        each other, showing up as tasks that pass confirmation for no visible reason.
        """

        model_config = ConfigDict(arbitrary_types_allowed=True)
        guard: Any = None

    class GuardedAgent(LLMAgent):  # type: ignore[misc]
        """Upstream LLMAgent wrapped in this project's guard.

        Only `generate_next_message` is overridden: the model produces tool calls as
        usual and each one is judged before it is issued. A call that is not allowed is
        never sent; an explanation goes to the user instead.
        """

        def __init__(self, tools: Any, domain_policy: str, llm: str, llm_args: Any = None) -> None:
            super().__init__(tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args)
            self._known_tools = {t.name for t in tools}

        def get_init_state(self, message_history: Any = None) -> Any:
            base = super().get_init_state(message_history)
            return GuardedState(
                system_messages=base.system_messages,
                messages=base.messages,
                guard=GuardState(),
            )

        def generate_next_message(self, message: Any, state: Any) -> Any:
            guard: GuardState = state.guard

            # 1) Learn from the incoming message: tool results give identity and
            #    ownership, a user reply consumes the pending confirmation.
            for tool_message in _incoming_tool_messages(message, MultiToolMessage, ToolMessage):
                name = guard.call_names.get(tool_message.id)
                if name and tool_message.content and not tool_message.error:
                    observe_tool_result(guard, name, _clean(tool_message.content))
            if isinstance(message, UserMessage) and guard.pending is not None:
                if is_affirmative(message.content or ""):
                    guard.confirmed.add(guard.pending.fingerprint)
                guard.pending = None

            # 2) Let upstream generate as usual.
            assistant = self._generate_next_message(message, state)

            # 3) Judge every call before it goes out.
            calls = list(assistant.tool_calls or [])
            if not calls:
                state.messages.append(assistant)
                return assistant, state

            blocked: list[str] = []
            for call in calls:
                proposed = ProposedCall(call.name, dict(call.arguments or {}))
                verdict = review_call(guard, self._known_tools, proposed)
                if verdict.allowed:
                    continue
                blocked.append(verdict.reason)
                guard.interventions.append(
                    {"tool": call.name, "decision": str(verdict.decision), "reason": verdict.reason}
                )
                if verdict.decision is Decision.NEEDS_CONFIRMATION:
                    guard.pending = proposed

            if not blocked:
                for call in calls:
                    guard.call_names[call.id] = call.name
                state.messages.append(assistant)
                return assistant, state

            # If any call in a message is blocked, the message carries no tool calls at
            # all: upstream requires a turn to be either text or tool calls, and
            # letting half through would make the explanation the user sees disagree
            # with what happened.
            #
            # cost/usage/raw_data must be carried over. The model call really happened
            # and really cost money; withholding the tool call is not a refund.
            # Upstream `get_cost()` goes further: if any assistant message in the
            # trace has cost None, the whole run's agent_cost becomes None, which
            # silently disables the M0.5 cross-check against litellm's independent
            # accounting. The only symptom is a cost of 0. The first smoke run hit this.
            held = AssistantMessage.text(
                "\n\n".join(blocked),
                cost=assistant.cost,
                usage=assistant.usage,
                raw_data=assistant.raw_data,
                generation_time_seconds=assistant.generation_time_seconds,
            )
            state.messages.append(held)
            return held, state

    return GuardedAgent


def _incoming_tool_messages(message: Any, multi_cls: Any, single_cls: Any) -> list[Any]:
    if isinstance(message, multi_cls):
        return list(message.tool_messages)
    if isinstance(message, single_cls):
        return [message]
    return []


def register(force: bool = False) -> str:
    """Register the guarded agent with the upstream registry and return its name.

    Upstream raises on duplicate registration, so this is idempotent: a script may
    call it more than once in the same process.
    """
    from tau2.registry import registry

    if registry.get_agent_factory(AGENT_NAME) is not None and not force:
        return AGENT_NAME

    agent_class = build_agent_class()

    def factory(tools: Any, domain_policy: str, llm: str, llm_args: Any = None, **_: Any) -> Any:
        # The factory is also passed task / audio arguments this agent does not use.
        return agent_class(tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args)

    registry.register_agent_factory(factory, AGENT_NAME)
    return AGENT_NAME
