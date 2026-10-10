"""Handing a session to a human: the ticket, and what the customer is told (M4).

Shared by the agent and the MCP server (M9), so both entry points open the same kind of
ticket and describe it in the same words. While the ticket is open the session runs no
tool on its own; each entry point enforces that on its own path, using `waiting_reply`.
"""

from __future__ import annotations

from retail_support_agent.store import Store, TicketSummary


def hand_off(
    store: Store | None,
    session_id: str | None,
    user_id: str,
    summary: str,
    requested_by: str | None,
) -> TicketSummary | None:
    """Turn a transfer into a ticket a human owns, or None without a database.

    Without a database a handoff is what it was before M4: a sentence. A second handoff
    on a session that is already waiting returns the ticket it already has.
    """
    if store is None or session_id is None:
        return None
    return store.open_ticket(session_id, user_id, summary, requested_by)


def handed_off_reply(ticket: TicketSummary | None) -> str:
    if ticket is None:
        return "Handed off to a human agent."
    return (
        f"Handed off to a human agent. Ticket #{ticket.id} is open; "
        "a person will take it from here."
    )


def waiting_reply(ticket: TicketSummary) -> str:
    return (
        f"Ticket #{ticket.id} is with a human agent. "
        "You will get an answer here once they have looked at it."
    )
