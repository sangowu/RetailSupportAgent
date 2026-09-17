"""Which slice of the retail data one session may touch.

The upstream dataset has no slice column. `db.json` carries `user_id`, `status`,
`address`, `items` and `payment_history`, and nothing that says "tenant", "sensitivity"
or "as of". It is also SHA256-verified (`scripts/fetch_tau2.py`), so a slice marker
cannot be written into it. Two consequences shape this module:

1. Row slices are *derived*, never stored. `slice_key` is a pure function of fields the
   record already has, so the same record yields the same slice on every run and a
   rebuilt session needs no extra state to reach the same verdict.
2. Column slices need no marker at all. A field name is its own sensitivity label:
   `email` and `payment_methods` are sensitive because of what they are, not because a
   column somewhere says so.

A scope is data, not behaviour: `WriteGuard` asks it questions and `RetailAgent` applies
its projection. That keeps every slice decision in one place a test can construct
directly, without a model, a database or a network.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

# Catalog and arithmetic. These carry no user or order id, describe stock shared by
# every customer, and are what the model needs to answer "what colours does this come
# in". Slicing them would deny the reads that make the domain usable while protecting
# nothing: the product table is identical for every account.
CATALOG_TOOLS: frozenset[str] = frozenset(
    {"get_product_details", "get_item_details", "list_all_product_types", "calculate"}
)

# Lookups that turn an email address, or a name and zip, into a user id. In the
# benchmark this is how the agent learns who it is talking to. In the application the
# identity already came from a verified token, so the same call is an enumeration
# primitive pointed at other people's accounts, and an enforcing scope refuses it.
DISCOVERY_TOOLS: frozenset[str] = frozenset({"find_user_id_by_email", "find_user_id_by_name_zip"})

# Fields hidden from the model by a redacting scope. Names, not markers: `email` is
# personal data wherever it appears, and `payment_methods` carries the card brand and
# last four digits. `address` is deliberately absent - the domain's own workflows change
# and verify addresses, so hiding it would break the task rather than protect anything.
SENSITIVE_FIELDS: frozenset[str] = frozenset({"email", "payment_methods"})

REDACTED = "[redacted by access scope]"


def slice_key(order: Mapping[str, Any]) -> tuple[str, str, str]:
    """The slice an order belongs to, derived from what the record already says.

    `(user_id, status, state)`: who owns it, how far along it is, and where it ships.
    The middle element is the one with teeth - "pending orders only" is a real retail
    control, not a demonstration artifact.

    A missing field yields `""` rather than raising: the caller is deciding access, and
    an exception mid-check is a harder failure to reason about than a slice that matches
    nothing.
    """
    address = order.get("address")
    state = address.get("state", "") if isinstance(address, Mapping) else ""
    return (str(order.get("user_id", "")), str(order.get("status", "")), str(state))


@dataclass(frozen=True)
class AccessScope:
    """The slice one session may reach, and what it may see of it.

    `principals` is the set of retail identities in scope - one for a customer acting on
    their own orders, one more for an agent working an assigned case. `statuses` narrows
    writes to an order lifecycle stage; `None` means every stage. `hidden_fields` is the
    column slice, applied to tool output before the model reads it.

    `source` records *why* this scope exists (`self`, `ticket:12`), because an audit row
    that says a call was allowed without saying under which authority cannot be reviewed.
    """

    principals: frozenset[str] = frozenset()
    statuses: frozenset[str] | None = None
    hidden_fields: frozenset[str] = frozenset()
    # Off by default. The benchmark adapter shares `WriteGuard` with the application and
    # discovers identity mid-conversation, so a scope that enforced reads by default
    # would deny every lookup made before `find_user_id_*` returned. The application
    # turns it on explicitly, where the identity arrives with the request.
    enforce_reads: bool = False
    source: str = "default"

    @classmethod
    def permissive(cls) -> AccessScope:
        """No row slice, no column slice, reads unenforced. The prior behaviour."""
        return cls()

    @classmethod
    def for_user(
        cls,
        user_id: str,
        *,
        statuses: frozenset[str] | None = None,
        hidden_fields: frozenset[str] = frozenset(),
        source: str = "self",
    ) -> AccessScope:
        """The slice of one retail identity."""
        return cls(
            principals=frozenset({user_id}),
            statuses=statuses,
            hidden_fields=hidden_fields,
            enforce_reads=True,
            source=source,
        )

    def including(self, user_id: str, *, source: str) -> AccessScope:
        """This scope widened to a second identity, as an assigned case does.

        Returns a new scope: widening in place would let a released assignment keep the
        access it granted, which is the failure `apply_context` exists to prevent.
        """
        return replace(self, principals=self.principals | {user_id}, source=source)

    def covers_user(self, user_id: str) -> bool:
        return not self.enforce_reads or user_id in self.principals

    def covers_status(self, status: str | None) -> bool:
        """Whether a write may touch an order at this lifecycle stage.

        An unknown status fails closed when a status slice is set: "the stage could not
        be determined" is not a reason to allow a write.
        """
        if self.statuses is None:
            return True
        return status is not None and status in self.statuses

    @property
    def redacts(self) -> bool:
        return bool(self.hidden_fields)

    def project(self, content: str) -> str:
        """Hide the column slice in one tool result.

        Input is the serialized tool output, so non-JSON results (upstream returns bare
        strings from several tools) pass through untouched: there is no field to hide in
        a string, and a parse failure must not turn a working read into an error.
        """
        if not self.hidden_fields:
            return content
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return content
        return json.dumps(_redact(payload, self.hidden_fields), ensure_ascii=False, default=str)


def _redact(value: Any, hidden: frozenset[str]) -> Any:
    """Replace hidden keys at any depth. Structure is kept; only values are replaced.

    Keeping the key visible is deliberate: the model must be able to say "I can see you
    have a card on file but not its number". Dropping the key instead makes the model
    report the field as absent, which is a different and wrong answer.
    """
    if isinstance(value, Mapping):
        return {
            key: REDACTED if key in hidden else _redact(item, hidden) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, hidden) for item in value]
    return value


@dataclass
class SliceAudit:
    """Slice verdicts made in one session, in order. Read by the API and by tests.

    A slice rule that cannot be shown to have fired is a slice rule nobody can review,
    so every enforced decision appends a row here, allowed ones included.
    """

    rows: list[dict[str, str]] = field(default_factory=list)

    def record(self, tool: str, verdict: str, reason: str, source: str) -> None:
        self.rows.append({"tool": tool, "verdict": verdict, "reason": reason, "source": source})
