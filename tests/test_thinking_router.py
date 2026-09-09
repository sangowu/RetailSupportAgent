"""M5.3: choosing the thinking mode per turn.

The router exists because M5.2 measured both fixed answers and found each wrong in a
different way. Its triggers are the two failure modes that were actually observed, so
these tests are written against those observations rather than against an idea of what a
hard turn looks like.

The costliest way for this step to be wrong is not a missed trigger. It is a re-run whose
first attempt is not paid for: the saving would then be measured against a bill that
omits half the calls.
"""

from __future__ import annotations

import pytest

from retail_support_agent.bench_agent import (
    TRIGGER_AFTER_REFUSAL,
    TRIGGER_QUANTITY,
    TRIGGER_WRITE,
    _sum_usage,
    observe_tool_result,
    thinking_trigger,
)

KNOWN = [
    "get_order_details",
    "find_user_id_by_email",
    "cancel_pending_order",
    "get_product_details",
]


def order_payload(order_id: str, user_id: str) -> str:
    return f'{{"order_id": "{order_id}", "user_id": "{user_id}"}}'


class TestWhichTurnsAreWorthThinkingAbout:
    def test_a_write_is_always_worth_it(self) -> None:
        """Six of the ten tasks lost without thinking failed the database check."""
        assert thinking_trigger("", ["cancel_pending_order"], False) == TRIGGER_WRITE

    def test_a_counted_claim_is_worth_it(self) -> None:
        """The measured failure: the tool result was in the conversation and the agent
        answered 11 and 12 where the answer was 10."""
        assert (
            thinking_trigger("There are 11 t-shirt options available.", [], False)
            == TRIGGER_QUANTITY
        )
        assert thinking_trigger("I found 12 options in stock.", [], False) == TRIGGER_QUANTITY

    def test_a_turn_after_a_refusal_is_worth_it(self) -> None:
        assert thinking_trigger("Let me try again.", [], True) == TRIGGER_AFTER_REFUSAL

    def test_an_ordinary_turn_is_not(self) -> None:
        assert thinking_trigger("Sure, let me look that order up.", [], False) is None

    def test_a_read_is_not_a_write(self) -> None:
        assert thinking_trigger("", ["get_order_details"], False) is None

    def test_identifiers_and_money_are_not_counts(self) -> None:
        """Order ids and totals are copied from a tool result, not derived from it.
        Triggering on them would re-run most turns in the run and cost more than never
        turning thinking off."""
        assert thinking_trigger("Your order #W5199551 totals $3,131.10.", [], False) is None
        assert thinking_trigger("It was delivered on 2026-05-14.", [], False) is None

    def test_a_write_wins_over_the_other_triggers(self) -> None:
        """One turn is re-run once, whatever it trips."""
        assert (
            thinking_trigger("There are 3 items.", ["cancel_pending_order"], True) == TRIGGER_WRITE
        )


class TestBothAttemptsArePaidFor:
    def test_usage_is_the_sum_of_the_two_calls(self) -> None:
        first = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 80},
            "completion_tokens_details": {"reasoning_tokens": 0},
        }
        second = {
            "prompt_tokens": 100,
            "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 90},
            "completion_tokens_details": {"reasoning_tokens": 250},
        }
        merged = _sum_usage(first, second)
        assert merged is not None
        assert merged["prompt_tokens"] == 200
        assert merged["completion_tokens"] == 320
        assert merged["prompt_tokens_details"]["cached_tokens"] == 170
        assert merged["completion_tokens_details"]["reasoning_tokens"] == 250

    def test_a_missing_record_does_not_erase_the_other(self) -> None:
        assert _sum_usage(None, {"prompt_tokens": 5}) == {"prompt_tokens": 5}
        assert _sum_usage({"prompt_tokens": 5}, None) == {"prompt_tokens": 5}


class TestTheAgentUnderTest:
    """These need tau2, which lives in the bench group."""

    def _agent(self, monkeypatch: pytest.MonkeyPatch, replies: list[object], boost: bool) -> object:
        import tau2.agent.llm_agent as llm_agent_module

        from retail_support_agent.bench_agent import build_agent_class

        remaining = list(replies)
        monkeypatch.setattr(llm_agent_module, "generate", lambda **_: remaining.pop(0))

        class FakeTool:
            def __init__(self, name: str) -> None:
                self.name = name

        agent_class = build_agent_class()
        return agent_class(
            tools=[FakeTool(name) for name in KNOWN],
            domain_policy="(test policy)",
            llm="openai/fake",
            llm_args={},
            boost_args={"extra_body": {"thinking": {"type": "enabled"}}} if boost else None,
        )

    def test_without_routing_the_turn_runs_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The default subject is exactly what M5.2 measured. A second call here would
        make every earlier baseline incomparable."""
        pytest.importorskip("tau2", reason="tau2 is in the bench group; skip if absent")
        from tau2.data_model.message import AssistantMessage, UserMessage

        cheap = AssistantMessage.text("There are 11 options.", cost=0.1)
        agent = self._agent(monkeypatch, [cheap], boost=False)
        state = agent.get_init_state()  # type: ignore[attr-defined]

        reply, state = agent.generate_next_message(  # type: ignore[attr-defined]
            UserMessage(role="user", content="how many?"), state
        )
        assert reply.content == "There are 11 options."
        assert state.guard.routing == []

    def test_a_triggered_turn_is_run_again_and_the_second_answer_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("tau2", reason="tau2 is in the bench group; skip if absent")
        from tau2.data_model.message import AssistantMessage, UserMessage

        cheap = AssistantMessage.text(
            "There are 11 options.", cost=0.1, usage={"prompt_tokens": 100, "completion_tokens": 8}
        )
        thought = AssistantMessage.text(
            "There are 10 options.", cost=0.4, usage={"prompt_tokens": 100, "completion_tokens": 90}
        )
        agent = self._agent(monkeypatch, [cheap, thought], boost=True)
        state = agent.get_init_state()  # type: ignore[attr-defined]

        reply, state = agent.generate_next_message(  # type: ignore[attr-defined]
            UserMessage(role="user", content="how many?"), state
        )

        assert reply.content == "There are 10 options."
        assert reply.cost == pytest.approx(0.5), "the discarded attempt was still bought"
        assert reply.usage["completion_tokens"] == 98
        assert state.guard.routing == [
            {
                "trigger": TRIGGER_QUANTITY,
                "changed": True,
                "first_tools": [],
                "second_tools": [],
            }
        ]

    def test_an_untriggered_turn_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("tau2", reason="tau2 is in the bench group; skip if absent")
        from tau2.data_model.message import AssistantMessage, UserMessage

        cheap = AssistantMessage.text("Let me look that up for you.", cost=0.1)
        agent = self._agent(monkeypatch, [cheap], boost=True)
        state = agent.get_init_state()  # type: ignore[attr-defined]

        reply, state = agent.generate_next_message(  # type: ignore[attr-defined]
            UserMessage(role="user", content="hello"), state
        )
        assert reply.cost == pytest.approx(0.1)
        assert state.guard.routing == []

    def test_a_write_is_re_run_before_the_guard_sees_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard judges the answer that will actually be sent, so the re-run has to
        happen first. A confirmed write must still be confirmed after it."""
        pytest.importorskip("tau2", reason="tau2 is in the bench group; skip if absent")
        from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage

        call = ToolCall(id="c1", name="cancel_pending_order", arguments={"order_id": "#W1"})
        cheap = AssistantMessage.text("", tool_calls=[call], cost=0.1)
        thought = AssistantMessage.text("", tool_calls=[call], cost=0.4)
        agent = self._agent(monkeypatch, [cheap, thought], boost=True)
        state = agent.get_init_state()  # type: ignore[attr-defined]
        observe_tool_result(state.guard, "find_user_id_by_email", "ava_moore_2033")
        observe_tool_result(
            state.guard, "get_order_details", order_payload("#W1", "ava_moore_2033")
        )

        held, state = agent.generate_next_message(  # type: ignore[attr-defined]
            UserMessage(role="user", content="cancel it"), state
        )

        assert not held.tool_calls, "the guard still holds an unconfirmed write"
        assert held.cost == pytest.approx(0.5), "both attempts are still paid for"
        assert state.guard.routing[0]["trigger"] == TRIGGER_WRITE
        assert state.guard.routing[0]["changed"] is False, "the same call twice is a wasted re-run"
        assert state.guard.last_turn_refused is True
