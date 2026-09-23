"""Every state the page can be in, and what it tells the visitor to do (M7.2).

The two tests that matter here derive their cases from the types rather than from a list
written by hand: every status `TurnResult` can carry, and every exception the client can
raise. A list would go stale in the direction that caused this step - three statuses had
no rendering at all and reached the page as their internal names, and nothing noticed
because nothing was enumerating them.
"""

from __future__ import annotations

import ast
import typing
from pathlib import Path

import pytest

from retail_support_agent import ui
from retail_support_agent.agent import TurnResult
from retail_support_agent.client import (
    ApiError,
    ConflictError,
    ForbiddenError,
    Reply,
    SessionBusyError,
    UnauthenticatedError,
    UnavailableError,
)
from retail_support_agent.ui import (
    TURN_STATES,
    Situation,
    render,
    situation_of,
    situation_of_failure,
)

# Resolved, not read raw: agent.py uses `from __future__ import annotations`, so the raw
# annotation is the string "Literal[...]" and `get_args` on it returns nothing. The guard
# test below exists because that failure is silent - every enumerated test would have
# passed over an empty set.
STATUSES = typing.get_args(typing.get_type_hints(TurnResult)["status"])


def descendants(cls: type) -> set[type]:
    found = set(cls.__subclasses__())
    for child in list(found):
        found |= descendants(child)
    return found


class TestEveryStatusIsRendered:
    def test_the_statuses_are_read_from_the_type(self) -> None:
        """If this ever finds nothing, the two tests below would pass vacuously."""
        assert len(STATUSES) >= 7
        assert "needs_confirmation" in STATUSES

    @pytest.mark.parametrize("status", STATUSES)
    def test_each_one_has_its_own_rendering(self, status: str) -> None:
        assert status in TURN_STATES, f"{status} would reach the page as its internal name"

    def test_no_two_statuses_look_the_same(self) -> None:
        """Distinct states that read identically are one state as far as a reader is
        concerned."""
        labels = [TURN_STATES[status].label for status in STATUSES]
        assert len(set(labels)) == len(labels)

    def test_nothing_is_rendered_that_cannot_happen(self) -> None:
        assert set(TURN_STATES) == set(STATUSES)


class TestEveryFailureIsRendered:
    def test_each_client_exception_is_told_apart(self) -> None:
        """Keyed on the class, because the class is what the client already decided the
        failure means, and two places deciding that is one too many."""
        labels = {situation_of_failure(cls("something")).label for cls in descendants(ApiError)}
        assert len(labels) == len(descendants(ApiError))

    def test_the_base_error_still_says_something_useful(self) -> None:
        situation = situation_of_failure(ApiError("the service rejected that request"))
        assert situation.advice
        assert "rejected" in situation.detail

    @pytest.mark.parametrize(
        ("failure", "expected"),
        [
            (UnauthenticatedError("expired"), "Sign in again"),
            (ForbiddenError("Missing permission: ticket:read"), "permission"),
            (SessionBusyError("already handling a message"), "Wait for it to finish"),
            (
                ConflictError("That request expired and must be raised again."),
                "trying again will not change it",
            ),
            (UnavailableError("boom"), "not your account"),
        ],
    )
    def test_each_one_says_what_to_do(self, failure: ApiError, expected: str) -> None:
        assert expected in situation_of_failure(failure).advice

    def test_only_an_expired_token_signs_the_visitor_out(self) -> None:
        """A 403 is not a reason to throw away a session: the account is signed in, and it
        may well be allowed to do the next thing."""
        signing_out = {
            cls.__name__
            for cls in descendants(ApiError)
            if situation_of_failure(cls("x")).signed_out
        }
        assert signing_out == {"UnauthenticatedError"}


class TestTheTwoConflictsDoNotShareAnAnswer:
    """A supervisor who approved an escalation after its window ran out was told the
    session was busy and to send again once it finished. Nothing was busy, nothing was
    coming, and sending again would never have worked.

    The status and the service's sentence are the same shape for both, so the page cannot
    tell them apart by looking: the client decides, and these pin what each becomes.
    """

    def test_an_expired_request_is_not_described_as_busy(self) -> None:
        situation = situation_of_failure(
            ConflictError("That request expired and must be raised again.")
        )
        assert "busy" not in situation.label
        assert "busy" not in situation.advice
        assert "expired" in situation.detail

    def test_no_permanent_conflict_tells_the_reader_to_wait(self) -> None:
        advice = situation_of_failure(ConflictError("That ticket is already closed.")).advice
        assert "Wait" not in advice

    def test_a_busy_session_still_says_to_wait(self) -> None:
        """The advice was right for the one conflict it was written for, and stays."""
        assert "Wait for it to finish" in situation_of_failure(SessionBusyError("busy")).advice

    def test_being_a_subclass_does_not_make_them_the_same_state(self) -> None:
        """`SessionBusyError` is a `ConflictError`, so a check in the wrong order renders
        both as one. Order is the whole fix, and nothing else in the file pins it."""
        busy = situation_of_failure(SessionBusyError("busy"))
        gone = situation_of_failure(ConflictError("expired"))
        assert busy.label != gone.label


class TestTheBoxIsPartOfTheState:
    def test_the_only_state_that_closes_the_box_is_a_finished_session(self) -> None:
        closed = {status for status, s in TURN_STATES.items() if not s.can_send}
        assert closed == {"stopped"}

    @pytest.mark.parametrize("status", ("answered", "needs_confirmation", "denied"))
    def test_a_state_that_can_be_advanced_leaves_it_open(self, status: str) -> None:
        assert TURN_STATES[status].can_send

    @pytest.mark.parametrize("status", ("handed_off", "waiting_for_human"))
    def test_waiting_on_a_person_leaves_the_box_open(self, status: str) -> None:
        """Found in the deployed demo. Closing the box here deadlocked the page: a case
        is resolved by somebody else, nothing pushes that to this page, and the only
        state that could have discovered it was the one the closed box prevented. The
        customer sat in front of a disabled box being told an answer was on its way.

        Sending while a case is open is free - `RetailAgent._waiting_for_human` answers
        before any model call or tool run - so the message is both how the page asks and
        how it resumes once the case is closed.
        """
        assert TURN_STATES[status].can_send

    @pytest.mark.parametrize("status", ("handed_off", "waiting_for_human"))
    def test_waiting_on_a_person_does_not_promise_delivery(self, status: str) -> None:
        """Nothing pushes to this page. "Their answer arrives here" was a promise the
        page had no mechanism to keep, and it read as an unanswered case forever."""
        assert "arrives here" not in TURN_STATES[status].advice

    def test_every_closed_state_says_what_to_do_instead(self) -> None:
        """Closing the box without saying why reads as a broken page."""
        for status, situation in TURN_STATES.items():
            if not situation.can_send:
                assert situation.advice, status

    def test_a_failure_never_closes_the_box(self) -> None:
        """A failed call is not a state of the conversation. The next message may work,
        and the one failure that is not recoverable signs the visitor out instead."""
        for cls in descendants(ApiError):
            situation = situation_of_failure(cls("x"))
            assert situation.can_send, cls.__name__


class TestWhatTheStatusLineShows:
    def test_a_held_write_shows_the_arguments_it_is_holding(self) -> None:
        """The arguments are the guard's claim. A summary would be the page asserting
        something the service did not say."""
        held = Reply(
            reply="This needs your confirmation.",
            status="needs_confirmation",
            pending_confirmation=(
                "cancel_pending_order(order_id='#W001', reason='no longer needed')"
            ),
        )
        line = render(situation_of(held))
        assert "#W001" in line
        assert "no longer needed" in line
        assert "Reply yes" in line

    def test_a_refusal_shows_the_guard_s_own_words(self) -> None:
        refused = Reply(reply="Order #W002 belongs to bob.", status="denied")
        assert "belongs to bob" in render(situation_of(refused))

    def test_a_stopped_session_shows_why_it_stopped(self) -> None:
        stopped = Reply(reply="stopped", status="stopped", stop_reason="step limit reached")
        line = render(situation_of(stopped))
        assert "step limit reached" in line
        assert "Start a new session" in line

    def test_a_plain_answer_adds_no_advice(self) -> None:
        """Nothing to do is not a state worth a sentence about."""
        assert not situation_of(Reply(reply="Here you are.", status="answered")).advice

    def test_a_status_the_page_has_never_heard_of_is_shown_rather_than_hidden(self) -> None:
        """A page that silently ignores a state it does not know shows a conversation in a
        state nobody can see."""
        assert situation_of(Reply(reply="", status="something-new")).label == "something-new"

    def test_the_line_survives_an_empty_situation(self) -> None:
        assert render(Situation(label="answered")).strip() == "**Status: answered**"


class TestThePageIsWiredConsistently:
    """Every handler returns one tuple in the shape of `outputs`.

    A mismatch is not a test failure anywhere else: it is a runtime error the first time
    that particular handler fires, which for the failure paths can be a long time after
    the change. This has happened before, in M6.7.
    """

    @staticmethod
    def build_demo_body() -> ast.FunctionDef:
        tree = ast.parse(Path(ui.__file__).read_text(encoding="utf-8"))
        return next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "build_demo"
        )

    def test_every_shape_helper_matches_the_outputs_list(self) -> None:
        body = self.build_demo_body()
        outputs = next(
            node.value
            for node in ast.walk(body)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.List)
            and any(
                isinstance(target, ast.Name) and target.id == "outputs" for target in node.targets
            )
        )
        assert isinstance(outputs, ast.List)
        width = len(outputs.elts)
        assert width >= 8

        helpers = [
            node
            for node in ast.walk(body)
            if isinstance(node, ast.FunctionDef)
            and node.name in {"_signed_out", "_signed_in", "_stay"}
        ]
        assert len(helpers) == 3
        for helper in helpers:
            returned = self.returned_tuples(helper)
            assert returned, helper.name
            for tup in returned:
                assert self.width_of(tup, body) == width, helper.name

    @classmethod
    def width_of(cls, tup: ast.Tuple, body: ast.FunctionDef) -> int:
        """How many components a returned tuple actually fills.

        `*_sections(...)` is one element to the parser and ten to Gradio, so a starred
        call is resolved to the width of what that helper returns. Without this the check
        passes on a tuple that is nine components short.
        """
        total = 0
        for element in tup.elts:
            if not isinstance(element, ast.Starred):
                total += 1
                continue
            call = element.value
            assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name), ast.dump(call)
            inner = next(
                node
                for node in ast.walk(body)
                if isinstance(node, ast.FunctionDef) and node.name == call.func.id
            )
            widths = {cls.width_of(one, body) for one in cls.returned_tuples(inner)}
            assert len(widths) == 1, call.func.id
            total += widths.pop()
        return total

    def test_a_turn_never_writes_into_the_sign_in_area(self) -> None:
        """And a sign-out never writes into the conversation's.

        A stale sentence in the wrong place is how a page tells a visitor something untrue
        about where they are: an error about a message, sitting above a sign-in form.
        """
        body = self.build_demo_body()
        helpers = {
            node.name: node
            for node in ast.walk(body)
            if isinstance(node, ast.FunctionDef) and node.name in {"_signed_out", "_stay"}
        }
        sign_in_message, status = 3, 8
        for tup in self.returned_tuples(helpers["_stay"]):
            assert self.is_empty_string(tup.elts[sign_in_message])
        for tup in self.returned_tuples(helpers["_signed_out"]):
            assert self.is_empty_string(tup.elts[status])

    @staticmethod
    def returned_tuples(fn: ast.FunctionDef) -> list[ast.Tuple]:
        return [
            node.value
            for node in ast.walk(fn)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
        ]

    @staticmethod
    def is_empty_string(node: ast.expr) -> bool:
        return isinstance(node, ast.Constant) and node.value == ""
