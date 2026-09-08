"""Guard verdicts once the guard is plugged into the benchmark.

These tests are offline and unbilled and do not need tau2's message classes: they cover
the pure logic (identity, ownership learning, the three verdicts, confirmation). The
agent class itself needs tau2 and is covered by a separate importorskip test.

Worth testing separately because identity is no longer given in the benchmark; it is
learned from the conversation. Learning it wrong lets through a write that should have
been blocked, an error that raises nothing.
"""

from __future__ import annotations

import json

import pytest

from retail_support_agent.bench_agent import (
    AGENT_NAME,
    NO_IDENTITY_REASON,
    UNKNOWN_OWNER_REASON,
    GuardState,
    is_affirmative,
    observe_tool_result,
    review_call,
)
from retail_support_agent.guardrails import (
    Decision,
    ProposedCall,
    SessionState,
    WriteGuard,
)

KNOWN = {
    "get_order_details",
    "find_user_id_by_email",
    "cancel_pending_order",
    "return_delivered_order_items",
}


def has_han(text: str) -> bool:
    """Whether the text contains Han characters. Code-point range, no dependency."""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def order_payload(order_id: str, user_id: str) -> str:
    return json.dumps({"order_id": order_id, "user_id": user_id, "status": "pending"})


class TestIdentityIsLearnedFromTheConversation:
    def test_identity_starts_empty(self) -> None:
        assert GuardState().identity is None

    def test_identity_tool_result_establishes_it(self) -> None:
        guard = GuardState()
        observe_tool_result(guard, "find_user_id_by_email", "ava_moore_2033")
        assert guard.identity == "ava_moore_2033"

    def test_an_error_sentence_is_not_taken_as_an_identity(self) -> None:
        """When no user is found upstream returns prose, which must not become an id.

        Treating it as one makes the ownership check compare against a sentence, which
        never matches: apparently safe, in fact blocking every write for an
        unexplainable reason.
        """
        guard = GuardState()
        observe_tool_result(guard, "find_user_id_by_email", "Error: user not found")
        assert guard.identity is None

    def test_writes_are_denied_before_identity_is_established(self) -> None:
        """Writes before identification are denied, as the upstream policy requires."""
        guard = GuardState()
        verdict = review_call(
            guard, KNOWN, ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        )
        assert verdict.decision is Decision.DENIED_OWNERSHIP
        assert "has not been authenticated" in verdict.reason


class TestOwnershipIsLearnedNotGuessed:
    def test_unknown_order_is_denied_with_an_actionable_reason(self) -> None:
        """Deny when ownership was never looked up, and say what to do first.

        Allowing it would mean guessing ownership from the order id; denying without
        information would make the model retry unchanged and burn turns.
        """
        guard = GuardState()
        observe_tool_result(guard, "find_user_id_by_email", "ava_moore_2033")
        verdict = review_call(
            guard, KNOWN, ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        )
        assert verdict.decision is Decision.DENIED_OWNERSHIP
        assert "get_order_details" in verdict.reason

    def test_known_order_of_another_user_is_denied(self) -> None:
        guard = GuardState()
        observe_tool_result(guard, "find_user_id_by_email", "ava_moore_2033")
        observe_tool_result(guard, "get_order_details", order_payload("#W1", "someone_else_1"))
        verdict = review_call(
            guard, KNOWN, ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        )
        assert verdict.decision is Decision.DENIED_OWNERSHIP

    def test_malformed_order_payload_teaches_nothing(self) -> None:
        """An unparseable payload teaches nothing and is never guessed at: a wrong guess
        lets through a write that should have been blocked."""
        guard = GuardState()
        observe_tool_result(guard, "get_order_details", "not json at all")
        assert guard.order_owner == {}


class TestConfirmation:
    def _ready(self) -> GuardState:
        guard = GuardState()
        observe_tool_result(guard, "find_user_id_by_email", "ava_moore_2033")
        observe_tool_result(guard, "get_order_details", order_payload("#W1", "ava_moore_2033"))
        return guard

    def test_own_order_still_needs_confirmation(self) -> None:
        """Correct ownership is not authorization to write. This is the project's claim."""
        guard = self._ready()
        verdict = review_call(
            guard, KNOWN, ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        )
        assert verdict.decision is Decision.NEEDS_CONFIRMATION
        # The confirmation text lists the real arguments, not just "are you sure".
        assert "#W1" in verdict.reason

    def test_confirmed_fingerprint_is_allowed(self) -> None:
        guard = self._ready()
        call = ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        guard.confirmed.add(call.fingerprint)
        assert review_call(guard, KNOWN, call).allowed

    def test_confirmation_does_not_carry_to_different_arguments(self) -> None:
        """A confirmation binds one argument set; changing a field expires it.

        This is the core of the guard: if the model changes arguments after being
        confirmed (a different order, an extra item), it must ask again, or "the user
        agreed" gets used to endorse an operation nobody agreed to.
        """
        guard = self._ready()
        confirmed = ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        guard.confirmed.add(confirmed.fingerprint)

        observe_tool_result(guard, "get_order_details", order_payload("#W2", "ava_moore_2033"))
        other = ProposedCall("cancel_pending_order", {"order_id": "#W2"})
        assert review_call(guard, KNOWN, other).decision is Decision.NEEDS_CONFIRMATION

        extra = ProposedCall("cancel_pending_order", {"order_id": "#W1", "reason": "changed"})
        assert review_call(guard, KNOWN, extra).decision is Decision.NEEDS_CONFIRMATION

    def test_reads_pass_without_identity(self) -> None:
        """Reads must not require identity, or the agent could not look up an identity."""
        guard = GuardState()
        assert review_call(
            guard, KNOWN, ProposedCall("get_order_details", {"order_id": "#W1"})
        ).allowed


class TestAffirmative:
    @pytest.mark.parametrize(
        "text",
        [
            "yes",
            "Yes, please go ahead",
            "confirm",
            "That's correct",
            # A real reply from a dev run: the quoted reason contains "no longer".
            'Confirmed — cancel #W5199551, "no longer needed."',
            "OK, cancel it. Reason: no longer needed",
        ],
    )
    def test_positive(self, text: str) -> None:
        assert is_affirmative(text)

    @pytest.mark.parametrize(
        "text",
        [
            "no",
            "No, don't cancel that",
            "wait, let me check",
            "not yet",
            "Please do not proceed",
            "",
        ],
    )
    def test_negative(self, text: str) -> None:
        """A negation means no confirmation. One extra round trip is cheaper than
        reading "don't cancel" as consent."""
        assert not is_affirmative(text)

    def test_quoted_reason_containing_no_is_not_a_refusal(self) -> None:
        """Kept as its own test because a real infinite loop produced it, so it is not
        mistaken for an ordinary edge case and deleted.

        The simulated user replied `Confirmed — cancel #W5199551, "no longer needed."`,
        unambiguous agreement. Substring matching on "no" read it as a refusal: the
        confirmation never arrived, the model resent, the guard blocked again, and one
        task looped 93 times on the same step until it hit the step limit and scored 0.
        The score dropped and the log showed no error at all.
        """
        assert is_affirmative('Confirmed — cancel #W5199551, "no longer needed."')
        # A real refusal is still a refusal.
        assert not is_affirmative("No — that order is no longer needed, leave it")

    def test_unrelated_dont_does_not_veto_an_explicit_yes(self) -> None:
        """Also produced by a real lost task (full set, task 80), the same class of error.

        Facing a Chinese confirmation prompt, the simulated user replied:

            "my Spanish is decent but I don't think that's Spanish!
             ... If so, then YES — go ahead!"

        That "don't" is about language, not the action. Any "don't" in the message used
        to veto it, so the unambiguous "YES — go ahead!" was discarded, the exchange
        never ran, and the task scored 0.

        A negation now counts only when it points at an action. The bias stays toward
        reading a refusal: a false refusal costs one round trip, a false agreement is an
        unconfirmed write.
        """
        assert is_affirmative(
            "my Spanish is decent but I don't think that's Spanish! If so, then YES — go ahead!"
        )
        # A negation aimed at the action still vetoes, even next to a "yes".
        assert not is_affirmative("Yes, but don't cancel the order")
        assert not is_affirmative("Please do not proceed with that")


class TestConfirmationPromptLanguage:
    """The confirmation text must be English.

    The τ²-bench conversation is English throughout. A Chinese prompt made the simulated
    user first guess what language it was and then guess what was being asked; task 80
    lost its score that way. The text is part of the product and must not assume the
    reader knows Chinese.
    """

    def test_describe_is_english(self) -> None:
        text = ProposedCall("cancel_pending_order", {"order_id": "#W001"}).describe()
        assert "Run cancel_pending_order" in text
        assert "#W001" in text
        assert not has_han(text)

    def test_confirmation_reason_is_english(self) -> None:
        guard = WriteGuard(KNOWN, lambda _: "alice")
        verdict = guard.review(
            ProposedCall("cancel_pending_order", {"order_id": "#W001"}),
            SessionState(user_id="alice"),
        )
        assert verdict.decision is Decision.NEEDS_CONFIRMATION
        assert not has_han(verdict.reason)

    def test_denial_reasons_are_english(self) -> None:
        assert not has_han(NO_IDENTITY_REASON)
        assert not has_han(UNKNOWN_OWNER_REASON)


class TestRegistration:
    def test_agent_can_be_built_and_registered(self) -> None:
        """It must be constructible before it can run. This turns red if upstream changes
        the agent interface."""
        pytest.importorskip("tau2", reason="tau2 is in the bench group; skip if absent")
        from tau2.registry import registry

        from retail_support_agent.bench_agent import build_agent_class, register

        assert build_agent_class() is not None
        assert register() == AGENT_NAME
        assert registry.get_agent_factory(AGENT_NAME) is not None
        # Registration must be idempotent: upstream raises on a duplicate name.
        assert register() == AGENT_NAME


class TestBlockedTurnStillReportsItsCost:
    """Withholding a tool call is not a refund: the substituted message must carry the
    original cost and usage.

    Upstream `get_cost()` is unforgiving: if any assistant message in the trace has
    cost None, the whole run's agent_cost becomes None. Every task the guard touched
    would then record a cost of 0, and the M0.5 cross-check against litellm's
    independent accounting would stop working. The only symptom is a cost of 0, with no
    error. The first smoke run hit this.
    """

    def _agent(self, monkeypatch: pytest.MonkeyPatch, reply: object) -> object:
        pytest.importorskip("tau2", reason="tau2 is in the bench group; skip if absent")
        import tau2.agent.llm_agent as llm_agent_module

        from retail_support_agent.bench_agent import build_agent_class

        monkeypatch.setattr(llm_agent_module, "generate", lambda **_: reply)

        class FakeTool:
            def __init__(self, name: str) -> None:
                self.name = name

        agent_class = build_agent_class()
        return agent_class(
            tools=[FakeTool(name) for name in KNOWN],
            domain_policy="(test policy)",
            llm="openai/fake",
            llm_args={},
        )

    def test_held_message_keeps_cost_and_drops_tool_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # tau2 is in the bench group and CI installs with `uv sync --locked` without it.
        # This importorskip must be at the top of the test body: the one in the helper
        # comes too late to stop the test body's own import.
        pytest.importorskip("tau2", reason="tau2 is in the bench group; skip if absent")
        from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage

        wanted = AssistantMessage.text(
            "",
            tool_calls=[
                ToolCall(id="c1", name="cancel_pending_order", arguments={"order_id": "#W1"})
            ],
            cost=0.5,
            usage={"prompt_tokens": 10, "completion_tokens": 2},
        )
        agent = self._agent(monkeypatch, wanted)

        state = agent.get_init_state()  # type: ignore[attr-defined]
        observe_tool_result(state.guard, "find_user_id_by_email", "ava_moore_2033")
        observe_tool_result(
            state.guard, "get_order_details", order_payload("#W1", "ava_moore_2033")
        )

        held, _ = agent.generate_next_message(  # type: ignore[attr-defined]
            UserMessage(role="user", content="cancel it"), state
        )

        assert not held.tool_calls, "a blocked tool call must not be issued"
        assert held.cost == 0.5, "the model call really cost money; the cost must survive"
        assert held.usage == {"prompt_tokens": 10, "completion_tokens": 2}
        assert "needs your explicit confirmation" in (held.content or "")

    def test_allowed_call_passes_through_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A read passes through, down to the message object itself."""
        pytest.importorskip("tau2", reason="tau2 is in the bench group; skip if absent")
        from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage

        wanted = AssistantMessage.text(
            "",
            tool_calls=[ToolCall(id="c1", name="get_order_details", arguments={"order_id": "#W1"})],
            cost=0.1,
        )
        agent = self._agent(monkeypatch, wanted)
        state = agent.get_init_state()  # type: ignore[attr-defined]
        out, _ = agent.generate_next_message(  # type: ignore[attr-defined]
            UserMessage(role="user", content="what's my order?"), state
        )
        assert out is wanted
        assert out.tool_calls
