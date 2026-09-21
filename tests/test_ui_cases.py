"""Who sees which section, and what the page does with a case (M7.3).

M4 built handoff, assignment, approval and closure, and nothing could reach any of it.
What is tested here is the half the page owns: which controls an account is offered, and
whether the rows it renders are the service's answers rather than the page's guesses.

The other half - who may actually do these things - is the service's, and
`tests/test_auth.py` asserts it route by route. Hiding a control is not enforcement.
"""

from __future__ import annotations

import pytest

from retail_support_agent.auth import ROLE_PERMISSIONS, Permission
from retail_support_agent.client import Account, Approval, Ticket
from retail_support_agent.ui import (
    ASSIGN,
    CASE_COLUMNS,
    CASES,
    DECIDE,
    WORK,
    Visitor,
    approvals_table,
    assignee_choices,
    cases_table,
    describe,
    holds,
)

SECTIONS = {"Cases": CASES, "Work this case": WORK, "Assign": ASSIGN, "Approvals": DECIDE}


def account(*roles: str) -> Account:
    granted: set[Permission] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS[role]
    return Account(
        user_id="ava_moore_2033",
        roles=roles,
        permissions=tuple(sorted(str(p) for p in granted)),
        auth="required",
    )


class TestWhoSeesWhat:
    """The permissions come from the database and decide the page. The two absences in
    the supervisor's row are the point: they assign and decide and cannot work a case."""

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            ("customer", set()),
            ("agent", {"Cases", "Work this case"}),
            ("supervisor", {"Cases", "Assign", "Approvals"}),
        ],
    )
    def test_each_role_is_offered_exactly_its_own_controls(
        self, role: str, expected: set[str]
    ) -> None:
        who = account(role)
        offered = {name for name, permission in SECTIONS.items() if holds(who, permission)}
        assert offered == expected

    def test_a_supervisor_is_not_offered_the_work_control(self) -> None:
        """The rule this project keeps claiming: an approver who also performs the action
        is not an approver. Asserted against the role matrix, not against a copy of it."""
        assert Permission.TICKET_WORK not in ROLE_PERMISSIONS["supervisor"]
        assert not holds(account("supervisor"), WORK)

    def test_signed_out_holds_nothing(self) -> None:
        assert not any(holds(None, permission) for permission in SECTIONS.values())

    def test_the_unauthenticated_demo_identity_gets_no_case_sections(self) -> None:
        """With authentication off the service answers as `demo`, which holds no ticket
        permission. The page in that mode is a conversation and nothing else."""
        who = account("demo")
        assert not any(holds(who, permission) for permission in SECTIONS.values())

    def test_every_section_is_gated_on_a_permission_that_exists(self) -> None:
        """A section keyed on a string no role can hold would be invisible forever, and
        the test above would still pass."""
        real = {str(p) for p in Permission}
        assert set(SECTIONS.values()) <= real


class TestWhatTheTablesShow:
    def test_a_case_row_names_the_customer_and_the_assignee(self) -> None:
        rows = cases_table(
            [
                Ticket(
                    id=4,
                    session_id="s1",
                    user_id="ava_moore_2033",
                    state="open",
                    summary="refund to a different card",
                    assignee="sub-mia",
                )
            ]
        )
        assert rows == [
            ["4", "open", "ava_moore_2033", "refund to a different card", "sub-mia", "", "s1"]
        ]

    def test_a_case_row_names_the_session_it_belongs_to(self) -> None:
        """Without it the audit panel can only reach the session the viewer is in and any
        with an approval still pending. A case that was closed last week - the one a
        review actually asks about - had no route at all."""
        rows = cases_table(
            [Ticket(id=4, session_id="abc123", user_id="ava", state="closed", summary="x")]
        )
        assert rows[0][-1] == "abc123"

    def test_the_case_columns_match_what_a_row_produces(self) -> None:
        rows = cases_table(
            [Ticket(id=4, session_id="s1", user_id="ava", state="open", summary="x")]
        )
        assert len(rows[0]) == len(CASE_COLUMNS)

    def test_an_assignee_is_named_rather_than_shown_as_a_subject(self) -> None:
        """Found in the deployed demo. The dropdown offers "ivan_santos_6635 · agent",
        and the row it produced read `3275a474-60b1-70e8-41c0-d228e5d2bb10`. The page had
        the name in hand and showed the opaque id, which is the thing `assignee_choices`
        exists to avoid."""
        rows = cases_table(
            [
                Ticket(
                    id=4,
                    session_id="s1",
                    user_id="ava",
                    state="assigned",
                    summary="x",
                    assignee="sub-mia",
                )
            ],
            [
                Account(
                    user_id="ivan_santos_6635", roles=("agent",), permissions=(), subject="sub-mia"
                )
            ],
        )
        assert rows[0][4] == "ivan_santos_6635"

    def test_a_subject_nobody_can_resolve_is_still_shown(self) -> None:
        """An identifier that cannot be looked up beats an empty cell."""
        rows = cases_table(
            [
                Ticket(
                    id=4,
                    session_id="s1",
                    user_id="ava",
                    state="assigned",
                    summary="x",
                    assignee="sub-unknown",
                )
            ],
            [],
        )
        assert rows[0][4] == "sub-unknown"

    def test_an_unassigned_case_says_so_rather_than_showing_nothing(self) -> None:
        rows = cases_table(
            [Ticket(id=4, session_id="s1", user_id="ava", state="open", summary="x")]
        )
        assert rows[0][4] == "unassigned"

    def test_no_cases_is_no_rows_rather_than_a_failure(self) -> None:
        assert cases_table([]) == []

    def test_an_approval_shows_every_argument(self) -> None:
        """The arguments are what is being decided. A summary would be the page choosing
        which parts of a write a supervisor gets to see."""
        rows = approvals_table(
            [
                Approval(
                    id=2,
                    session_id="t4-abc",
                    tool_name="modify_pending_order_payment",
                    arguments={"order_id": "#W001", "payment_method_id": "gift_card_9"},
                    expires_at="2026-09-10T12:00:00",
                )
            ]
        )
        assert rows[0][1] == "modify_pending_order_payment"
        assert "#W001" in rows[0][2]
        assert "gift_card_9" in rows[0][2]


class TestPickingSomebodyToAssignTo:
    """A subject is an opaque identifier. Showing one and asking a supervisor to type it
    is the difference between a control that works and one that exists."""

    def test_a_choice_reads_as_a_person_and_carries_a_subject(self) -> None:
        choices = assignee_choices(
            [Account(user_id="ivan_santos_6635", roles=("agent",), subject="sub-mia")]
        )
        assert choices == [("ivan_santos_6635 · agent", "sub-mia")]

    def test_an_account_with_no_subject_is_not_offered(self) -> None:
        """`/me` never answers with a subject. An account that arrived from there would be
        offered as a choice that cannot be assigned to."""
        assert assignee_choices([Account(user_id="ava_moore_2033", roles=("agent",))]) == []

    def test_nobody_to_assign_to_is_an_empty_list(self) -> None:
        assert assignee_choices([]) == []


class TestTheAccountLine:
    def test_it_names_the_case_being_worked(self) -> None:
        """An agent acting on somebody else's orders inside their own session should not
        have to remember whose."""
        line = describe(account("agent"), working=4)
        assert "working case 4" in line

    def test_it_says_nothing_about_cases_when_none_is_being_worked(self) -> None:
        assert "working case" not in describe(account("customer"))


class TestTheVisitorCarriesWhatWasRead:
    def test_a_fresh_visitor_has_no_case_state(self) -> None:
        visitor = Visitor()
        assert visitor.cases == ()
        assert visitor.approvals == ()
        assert visitor.assignable == ()
        assert visitor.working is None

    def test_may_reads_the_account_the_service_returned(self) -> None:
        assert Visitor(account=account("supervisor")).may(DECIDE)
        assert not Visitor(account=account("supervisor")).may(WORK)
        assert not Visitor().may(CASES)
