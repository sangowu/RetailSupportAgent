"""The audit page: who is offered it, and what it renders.

The backend has recorded executed writes, refused access and altered answers since M8.4,
and until now nothing could read any of it. A supervisor held `audit:read` and had
nowhere to use it.

What is tested here is the page's half: the row shape, the vocabulary it shows, and that
the panel is offered exactly to accounts that hold the permission. Who may actually read
a trail is the service's half, and `tests/test_auth.py` asserts that on the route.
Hiding a panel is not enforcement.
"""

from __future__ import annotations

import pytest

from retail_support_agent.auth import ROLE_PERMISSIONS, Permission
from retail_support_agent.client import Account, AuditRecord
from retail_support_agent.ui import AUDIT, AUDIT_COLUMNS, audit_table, build_demo, holds


def account(*roles: str) -> Account:
    granted: set[Permission] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS[role]
    return Account(
        user_id="ava_moore_2033",
        roles=roles,
        permissions=tuple(sorted(str(p) for p in granted)),
        subject="sub-1",
    )


def record(kind: str, **detail: object) -> AuditRecord:
    return AuditRecord(kind=kind, detail=detail, created_at="2026-09-19T10:00:00+00:00")


class TestWhoIsOfferedTheTrail:
    @pytest.mark.parametrize("role", ["supervisor", "auditor"])
    def test_accounts_with_the_permission_are(self, role: str) -> None:
        assert holds(account(role), AUDIT)

    @pytest.mark.parametrize("role", ["customer", "agent", "demo"])
    def test_accounts_without_it_are_not(self, role: str) -> None:
        assert not holds(account(role), AUDIT)

    def test_the_auditor_role_has_the_trail_and_nothing_else(self) -> None:
        """The role exists to make the intersection testable: read the trail, touch no
        orders. If it ever gained order access the panel would stop being evidence of a
        least-privilege role."""
        auditor = account("auditor")
        assert holds(auditor, AUDIT)
        assert "order:read" not in auditor.permissions
        assert "order:write" not in auditor.permissions


class TestWhatARowSays:
    def test_a_refusal_names_the_tool_the_authority_and_the_reason(self) -> None:
        rows = audit_table(
            [
                record(
                    "access_denied",
                    tool="get_order_details",
                    authority="ticket:12",
                    reason="Order #W002 does not exist or its owner cannot be confirmed.",
                )
            ]
        )
        assert len(rows) == 1
        when, kind, tool, authority, detail = rows[0]
        assert kind == "access_denied"
        assert tool == "get_order_details"
        assert authority == "ticket:12"
        assert "#W002" in detail
        assert when.startswith("2026-09-19")

    def test_the_kind_is_shown_as_the_database_writes_it(self) -> None:
        """A page that renames `access_denied` to "Refused" gives the screen and the
        table two vocabularies, and the person grepping the database loses."""
        rows = audit_table([record("output_withheld", reason="identifiers not read")])
        assert rows[0][1] == "output_withheld"

    def test_an_executed_write_renders_without_the_refusal_fields(self) -> None:
        rows = audit_table(
            [record("write_executed", tool="cancel_pending_order", fingerprint="abc123")]
        )
        _, kind, tool, authority, detail = rows[0]
        assert kind == "write_executed"
        assert tool == "cancel_pending_order"
        assert authority == ""
        # The fingerprint is not a field this page was taught about, and it is shown
        # rather than dropped.
        assert "abc123" in detail

    def test_an_unknown_detail_field_is_not_swallowed(self) -> None:
        """The field the page has not been taught about is the one worth seeing."""
        rows = audit_table([record("write_executed", surprise="look at me")])
        assert "surprise" in rows[0][4]
        assert "look at me" in rows[0][4]

    def test_an_empty_trail_is_no_rows(self) -> None:
        assert audit_table([]) == []

    def test_the_columns_match_what_a_row_produces(self) -> None:
        rows = audit_table([record("access_denied", tool="x")])
        assert len(rows[0]) == len(AUDIT_COLUMNS)


class TestThePanelIsWiredLikeEveryOther:
    def test_the_audit_events_write_the_whole_page(self) -> None:
        """The first version of the handler wrote only its own two components. A 401
        there would have left the page showing a signed-in account with no token, and
        the audit panel is exactly where that matters.
        """
        demo = build_demo()
        widths = {len(block.outputs) for block in demo.fns.values() if block.outputs}
        # Still two widths: the password-clearing follow-up, and the whole page.
        assert len(widths) == 2, widths
