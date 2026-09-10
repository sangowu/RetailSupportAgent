"""Provisioning the demo's accounts from what Terraform wrote.

This runs unattended at every boot, so a mistake in the mapping surfaces as a 403 on a
machine somebody is demonstrating. The cases below are the ones that turn a mistake into a
failed boot instead, which is louder and points at the cause.
"""

from __future__ import annotations

import json

import pytest

from retail_support_agent.seed import Account, SeedError, apply, parse

AVA = {"subject": "8f14e45f-ea", "user_id": "ava_moore_2033", "roles": ["customer"]}


class Recorder:
    """A stand-in for the store. `provision_identity` is the only thing seeding uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def provision_identity(self, subject: str, user_id: str, roles: object = ()) -> None:
        self.calls.append((subject, user_id, tuple(roles)))  # type: ignore[arg-type]


class TestNothingToDo:
    @pytest.mark.parametrize("raw", (None, "", "   "))
    def test_an_absent_mapping_is_not_an_error(self, raw: str | None) -> None:
        """This is the local stack and every developer machine: no pool, nothing to map."""
        assert parse(raw) == []


class TestReading:
    def test_an_account_is_read_whole(self) -> None:
        assert parse(json.dumps([AVA])) == [
            Account(subject="8f14e45f-ea", user_id="ava_moore_2033", roles=("customer",))
        ]

    def test_an_account_with_no_roles_is_allowed(self) -> None:
        """An identity with no roles can authenticate and do nothing, which is a real
        state and not a mistake."""
        entry = {"subject": "s", "user_id": "u"}
        assert parse(json.dumps([entry]))[0].roles == ()

    @pytest.mark.parametrize(
        "raw",
        (
            "not json at all",
            '{"subject": "s"}',
            '[{"user_id": "ava_moore_2033"}]',
            '[{"subject": "s"}]',
            '[{"subject": "", "user_id": "u"}]',
            '["a string"]',
            '[{"subject": "s", "user_id": "u", "roles": "customer"}]',
        ),
    )
    def test_a_malformed_mapping_stops_the_boot(self, raw: str) -> None:
        """Loudly, rather than provisioning some of it.

        A partial mapping is the worst outcome: the accounts it skipped fail with a 403
        that is indistinguishable from an authorization defect.
        """
        with pytest.raises(SeedError):
            parse(raw)

    def test_the_message_says_which_entry(self) -> None:
        with pytest.raises(SeedError) as caught:
            parse(json.dumps([AVA, {"subject": "s"}]))
        assert "[1]" in str(caught.value)


class TestWhatCannotBeGranted:
    def test_a_role_that_does_not_exist_is_refused(self) -> None:
        """`permissions_for` ignores an unknown role, so a typo would produce an account
        with no powers and no explanation. Refusing names the typo."""
        entry = {"subject": "s", "user_id": "u", "roles": ["supervsior"]}
        with pytest.raises(SeedError) as caught:
            parse(json.dumps([entry]))
        assert "supervsior" in str(caught.value)

    def test_the_demo_role_is_refused(self) -> None:
        """It is what the service invents for itself when authentication is off, and it
        carries session:reset_all. Granting it to a signed-in visitor would let them drop
        everyone else's sessions.
        """
        entry = {"subject": "s", "user_id": "u", "roles": ["demo"]}
        with pytest.raises(SeedError) as caught:
            parse(json.dumps([entry]))
        assert "not grantable" in str(caught.value)


class TestWriting:
    def test_each_account_is_written_once(self) -> None:
        store = Recorder()
        accounts = [
            Account("sub-a", "ava_moore_2033", ("customer",)),
            Account("sub-b", "ivan_santos_6635", ("customer",)),
        ]
        apply(store, accounts)
        assert store.calls == [
            ("sub-a", "ava_moore_2033", ("customer",)),
            ("sub-b", "ivan_santos_6635", ("customer",)),
        ]

    def test_the_report_names_the_identity_and_not_a_count(self) -> None:
        """It is the boot log. "2 identities" cannot be checked against what was meant."""
        lines = apply(Recorder(), [Account("sub-a", "ava_moore_2033", ("customer",))])
        assert "ava_moore_2033" in lines[0]
        assert "customer" in lines[0]
