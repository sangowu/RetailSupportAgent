"""The demo UI: sign in, then talk to the API as the account you signed in as.

Until M6.7 this page built a `RetailAgent` in its own process under a hard-coded user id.
That made the demo the one path in the system that skipped authentication, session
ownership and the role table: whoever opened the page acted as `ava_moore_2033`. The page
now signs in against the user pool and calls the API with the resulting token, so what a
visitor may do is decided by the same code that decides it for any other caller.

Two rules carry the identity handling:

1. **The browser never receives a token.** Sign-in happens in this process and the tokens
   live in `gr.State`, which is per browser session and in this process's memory. Gradio
   splits one interaction into a POST and a server-sent event stream, so a credential the
   client had to attach would have to be attached to both; not giving it one removes the
   problem rather than solving it twice.
2. **Nothing here decides what an account may do.** The roles and permissions on the page
   are read from `/me`, which reads them from the database. This module renders them.

The three states a business action can end in - answered, awaiting confirmation, denied -
are still shown explicitly, because "writes require explicit confirmation" is this
project's central claim and must not be buried in conversation text.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

import gradio as gr

from retail_support_agent.client import (
    Account,
    Api,
    ApiError,
    Approval,
    AuditRecord,
    ConflictError,
    Event,
    ForbiddenError,
    Reply,
    Ticket,
    UnauthenticatedError,
    UnavailableError,
)
from retail_support_agent.cognito import Cognito, SignInError, Tokens


@dataclass(frozen=True)
class Situation:
    """What state the page is in, and the one thing to do about it (M7.2).

    The page used to report the last thing that happened and leave the reader to work out
    what that meant for them. A state and a remedy are different things from an event, and
    a visitor acts on the first two.
    """

    label: str
    # The service's own words, or the arguments the guard is holding. Never a paraphrase:
    # a 403 names the permission that was missing because `tests/test_auth.py` requires it
    # to, and a page that reworded that would become the thing people read instead.
    detail: str = ""
    # What to do next. Empty when the answer is simply to carry on.
    advice: str = ""
    # Whether another message can advance this state. Only `stopped` cannot: the session
    # is over and nothing sent into it can be answered.
    #
    # The two waiting states used to close the box too, on the reasoning that the only
    # possible answer was already on screen. That reasoning was wrong, and closing the
    # box deadlocked the page: a case is resolved by a person somewhere else, the page
    # has no way to be told, and the only state that could learn it was the one the box
    # had been closed to prevent. Sending while a case is open costs nothing - the agent
    # answers a suspended session before it calls a model or runs a tool - so the message
    # is how the page asks, and it is the only way it can ask.
    can_send: bool = True
    # Only an expired token, which is the one state that is not about this conversation.
    signed_out: bool = False


# One entry per status a turn can return. Enumerated against `TurnResult`'s own type in
# tests/test_ui_states.py, so a status added later fails the suite until the page has
# something to say about it. That check exists because three of these had no rendering at
# all and reached the page as their internal names.
TURN_STATES: dict[str, Situation] = {
    "answered": Situation(label="answered"),
    "needs_confirmation": Situation(
        label="⚠ waiting for your confirmation",
        advice="Reply yes to run exactly these arguments, or say what to change.",
    ),
    "needs_approval": Situation(
        label="⚠ waiting for a supervisor",
        advice="A supervisor has to decide this one. You can carry on with anything else.",
    ),
    "denied": Situation(label="✗ refused"),
    # Neither of these promises that an answer arrives on its own. Nothing pushes to this
    # page, and saying otherwise left a resolved case looking like an unanswered one.
    "handed_off": Situation(
        label="→ handed to a person",
        advice="A person owns this case now. Send another message to check whether they "
        "have finished with it.",
    ),
    "waiting_for_human": Situation(
        label="→ with a person",
        advice="Send again to check whether it has been dealt with, or start a new "
        "session to ask about something else.",
    ),
    "stopped": Situation(
        label="■ session stopped",
        advice="Start a new session to carry on.",
        can_send=False,
    ),
}


def situation_of(reply: Reply) -> Situation:
    """What state a completed turn leaves the page in."""
    base = TURN_STATES.get(reply.status, Situation(label=reply.status))
    detail = reply.pending_confirmation if reply.status == "needs_confirmation" else ""
    if reply.status == "denied":
        detail = reply.reply
    if reply.stop_reason:
        detail = reply.stop_reason
    return replace(base, detail=detail or "")


def situation_of_failure(exc: ApiError) -> Situation:
    """What state a failed call leaves the page in.

    Keyed on the exception class rather than on a status code, because the class is what
    the client already decided the failure means, and two places deciding that is one too
    many.
    """
    if isinstance(exc, UnauthenticatedError):
        return Situation(
            label="signed out",
            detail=str(exc),
            advice="Sign in again. The conversation was cleared: it belonged to a session "
            "this browser can no longer prove is yours.",
            signed_out=True,
        )
    if isinstance(exc, ForbiddenError):
        return Situation(
            label="✗ not permitted",
            detail=str(exc),
            advice="This account may not do that. Ask something else, or sign in as an "
            "account that holds the permission named above.",
        )
    if isinstance(exc, ConflictError):
        return Situation(
            label="✗ this session is open somewhere else",
            detail=str(exc),
            advice="Another process is driving this session. Start a new session.",
        )
    if isinstance(exc, UnavailableError):
        return Situation(
            label="✗ the service failed",
            detail=str(exc),
            advice="This is not your account. Try again in a moment.",
        )
    return Situation(label="✗ refused", detail=str(exc), advice="Try a different request.")


def render(situation: Situation) -> str:
    """The status line. One place, so the states cannot start looking like each other."""
    parts = [f"**Status: {situation.label}**"]
    if situation.detail:
        parts.append(f"\n\n`{situation.detail}`")
    if situation.advice:
        parts.append(f"\n\n{situation.advice}")
    return "".join(parts)


# While a turn is running. The status line is the only place the page reports state, so
# leaving the previous turn's status there would be a lie for as long as this one takes.
# The box stays open: a turn in progress is not a state that needs a remedy.
WORKING = Situation(label="working…")

# What the box invites, and what it says when nothing can be typed into it. A disabled box
# with a stale placeholder reads as broken rather than as finished.
OPEN_PLACEHOLDER = "e.g. show me my orders"
CLOSED_PLACEHOLDER = "this session cannot take another message"

# One line per step, in the assistant's bubble, replaced by the answer when it arrives.
# Rendered as an assistant message rather than beside the box, because the work belongs
# to the turn the reader is watching and moves up with it.
STEP_LABELS = {
    "thinking": "asking the model…",
    "tool": "ran {tool}",
    "held": "⚠ held {tool} for your confirmation",
    "approval": "⚠ {tool} is waiting for a supervisor",
    "refused": "✗ refused",
}


def _describe(event: Event) -> str:
    """One progress event, as a line somebody reads."""
    template = STEP_LABELS.get(event.kind, event.kind)
    line = template.format(tool=event.tool or "a tool")
    return f"{line} · {event.detail}" if event.kind == "refused" and event.detail else line


def _progress(steps: list[str]) -> dict[str, Any]:
    """The assistant's bubble while the turn is running."""
    shown = "\n".join(f"- {step}" for step in steps) if steps else "- working…"
    return {"role": "assistant", "content": shown}


INTRO = """### RetailSupportAgent

Order lookup, order cancellation and return requests, against τ²-bench simulated retail
data. No account here belongs to a real person.

Write actions are held for confirmation of their exact arguments. That is by design,
not a hang.
"""

SIGNED_OUT = "Signed out."


@dataclass(frozen=True)
class Visitor:
    """One browser session's state. Held in `gr.State`, never sent to the browser."""

    token: str | None = None
    refresh_token: str | None = None
    session_id: str = ""
    account: Account | None = None
    # What this account can see of the case work (M7.3). Held here rather than read on
    # every render, so one place decides what is on screen and a failed refresh leaves
    # the last known state visible instead of an empty table that looks like "no cases".
    cases: tuple[Ticket, ...] = ()
    approvals: tuple[Approval, ...] = ()
    assignable: tuple[Account, ...] = ()
    # The case whose session is loaded in the conversation, when one is.
    working: int | None = None

    @property
    def signed_in(self) -> bool:
        return self.account is not None

    def may(self, permission: str) -> bool:
        return holds(self.account, permission)


def api_from_env() -> Api:
    """Which service to call. Inside compose this is the API container by service name."""
    return Api(base_url=os.getenv("RSA_API_URL", "http://127.0.0.1:8000"))


def cognito_from_env() -> Cognito | None:
    """The user pool, or None when one is not configured.

    Unconfigured is the local stack and the developer machine: the API is then in its own
    unauthenticated mode and answers as the demo identity, so the page has nothing to sign
    in to and does not ask. Both halves have to be unconfigured together, which is why
    `/me` reports which mode the service is in and the page shows it.
    """
    region = os.getenv("RSA_COGNITO_REGION", "")
    client_id = os.getenv("RSA_COGNITO_CLIENT_ID", "")
    if not region or not client_id:
        return None
    return Cognito(region=region, client_id=client_id)


def describe(account: Account, working: int | None = None) -> str:
    """The account line on the page.

    It names the retail identity and the roles because they are the evidence: they come
    from the database for the signed-in subject, and no field on this page can change
    them.

    From M7.3 it also names the case being worked, when one is. An agent acting on a
    customer's orders inside their own session should not have to remember whose.
    """
    roles = ", ".join(account.roles) or "no roles"
    line = f"**Acting as** `{account.user_id}` · **roles:** {roles}"
    if working is not None:
        line += f" · **working case {working}**"
    if account.auth != "required":
        line += " · authentication is not configured on this service"
    return line


def authenticate(cognito: Cognito, api: Api, username: str, password: str) -> Visitor:
    """Sign in and read back who the service says that account is.

    Both halves, in this order. A token that verifies for a subject with no row in
    `identities` is authenticated and authorized to do nothing, and finding that out at
    sign-in is better than finding it out on the first message.
    """
    tokens: Tokens = cognito.sign_in(username.strip(), password)
    account = api.me(tokens.access_token)
    return Visitor(
        token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        session_id=uuid.uuid4().hex,
        account=account,
    )


def retrying(visitor: Visitor, cognito: Cognito | None, call: Any) -> tuple[Visitor, Any]:
    """Run `call(token)`, refreshing the access token once if it has expired.

    Once, not in a loop. A refresh token the pool has revoked would otherwise turn one
    expired session into a stream of requests to the pool, and the visitor would watch a
    page that never resolves instead of a form that asks them to sign in again.
    """
    try:
        return visitor, call(visitor.token)
    except UnauthenticatedError:
        if cognito is None or not visitor.refresh_token:
            raise
        try:
            tokens = cognito.refresh(visitor.refresh_token)
        except SignInError as exc:
            raise UnauthenticatedError(str(exc)) from None
        refreshed = replace(visitor, token=tokens.access_token)
        return refreshed, call(refreshed.token)


# ---- what an account may see (M7.3) -----------------------------------------
#
# Which sections a visitor gets comes from the permissions `/me` reports, which come from
# the database. Hiding is not enforcement: every route refuses a caller who lacks the
# permission, and `tests/test_auth.py` asserts that route by route. The page hides
# sections because offering an action that will be refused is a bad interface.
#
# The supervisor's two absences are the interesting part. They assign cases and decide
# escalations and cannot work a case, because an approver who also performs the action is
# not an approver.

CASES = "ticket:read"
WORK = "ticket:work"
ASSIGN = "ticket:assign"
DECIDE = "escalation:decide"
AUDIT = "audit:read"


def holds(account: Account | None, permission: str) -> bool:
    return account is not None and permission in account.permissions


def cases_table(tickets: Sequence[Ticket]) -> list[list[str]]:
    """The case list, as rows. Empty is a state worth rendering, not a blank panel.

    The session is shown because it is the only way to audit a case that is over. The
    audit panel reads a named session, and without this column the ids a supervisor can
    reach are the one they are sitting in and any with an approval still pending - so a
    case that was resolved last week, which is exactly the one a review asks about, had
    no route. The service already sends the id; the page was simply not rendering it.

    Showing it grants nothing: `/session/{id}/audit` checks `audit:read` on its own, and
    an agent sees only the cases assigned to them either way.
    """
    return [
        [
            str(ticket.id),
            ticket.state,
            ticket.user_id,
            ticket.summary,
            ticket.assignee or "unassigned",
            ticket.resolution or "",
            ticket.session_id,
        ]
        for ticket in tickets
    ]


CASE_COLUMNS = ["case", "state", "customer", "summary", "assignee", "resolution", "session"]


def approvals_table(approvals: Sequence[Approval]) -> list[list[str]]:
    """One row per escalation. The arguments are shown whole: they are what is being
    decided, and a summary would be the page deciding which parts matter."""
    return [
        [
            str(approval.id),
            approval.tool_name,
            ", ".join(f"{k}={v!r}" for k, v in sorted(approval.arguments.items())),
            approval.session_id,
            approval.expires_at,
        ]
        for approval in approvals
    ]


APPROVAL_COLUMNS = ["request", "action", "arguments", "session", "expires"]


def audit_table(records: Sequence[AuditRecord]) -> list[list[str]]:
    """The audit trail, as rows.

    `kind` is shown as written rather than prettified: `access_denied` is the value a
    reviewer will grep the database for, and a page that renames it to "Refused" makes
    the screen and the table two different vocabularies.

    `detail` varies by kind, so it is rendered generically. The interesting fields are
    pulled out where they exist - the tool, and the authority the session was acting
    under - and whatever is left is shown rather than dropped, because a field this page
    has not been taught about is exactly the one worth seeing.
    """
    rows: list[list[str]] = []
    for record in records:
        detail = dict(record.detail)
        tool = str(detail.pop("tool", detail.pop("tool_name", "")))
        authority = str(detail.pop("authority", ""))
        reason = str(detail.pop("reason", ""))
        rest = ", ".join(f"{key}={value!r}" for key, value in sorted(detail.items()))
        rows.append([record.created_at, record.kind, tool, authority, reason or rest])
    return rows


AUDIT_COLUMNS = ["when", "what happened", "action", "authority", "detail"]


def session_to_audit(typed: str, visitor: Visitor) -> str:
    """Which session the trail should be read for: what was typed, else this one.

    The panel first shipped requiring a session to be named, and a session id is state
    the page holds and never shows. A supervisor who had just been refused something had
    no way to learn the id of the conversation they were sitting in, so the permission
    still had nowhere to be used - the same dead end the panel was built to remove.
    Found by signing in to the deployed demo, not by a test.

    Blank therefore means this session. Another session is still reachable by naming it,
    which is what the box is for, and the note the handler writes names whichever was
    read, so the id becomes something a person can see and copy.
    """
    return (typed or "").strip() or visitor.session_id


def assignee_choices(accounts: Sequence[Account]) -> list[tuple[str, str]]:
    """Who a case can be given to, as (what a person reads, what the API needs).

    A subject is an opaque identifier. Showing one and asking a supervisor to pick it is
    the difference between a control that works and one that exists.
    """
    return [
        (f"{account.user_id} · {', '.join(account.roles) or 'no roles'}", account.subject or "")
        for account in accounts
        if account.subject
    ]


def build_demo() -> gr.Blocks:
    api = api_from_env()
    cognito = cognito_from_env()

    with gr.Blocks(title="RetailSupportAgent") as demo:
        gr.Markdown(INTRO)
        visitor_state = gr.State(Visitor())

        with gr.Column(visible=cognito is not None) as sign_in_panel:
            gr.Markdown("Sign in with the account you were given.")
            with gr.Row():
                username = gr.Textbox(label="Username", scale=2, autofocus=True)
                # `type="password"` keeps it out of the rendered value and out of the
                # browser's autofill history for a text field.
                password = gr.Textbox(label="Password", type="password", scale=2)
                sign_in = gr.Button("Sign in", variant="primary", scale=1)
            sign_in_message = gr.Markdown("")

        with gr.Column(visible=False) as app_panel:
            whoami = gr.Markdown("")
            chatbot = gr.Chatbot(height=440, label="Conversation")
            status = gr.Markdown("")
            with gr.Row():
                box = gr.Textbox(placeholder=OPEN_PLACEHOLDER, scale=5, show_label=False)
                send = gr.Button("Send", variant="primary", scale=1)
            with gr.Row():
                reset = gr.Button("New session")
                sign_out = gr.Button("Sign out", visible=cognito is not None)

            # M4 built these and nothing could reach them. Which of the three appears is
            # decided by the permissions `/me` reports, which the database decides. The
            # page hides what an account may not do because offering an action that will
            # be refused is a bad interface; the refusal itself lives in the service.
            with gr.Accordion("Cases", open=False, visible=False) as cases_panel:
                cases = gr.Dataframe(headers=CASE_COLUMNS, interactive=False, wrap=True, label=None)
                with gr.Row():
                    case_id = gr.Number(label="Case", precision=0, minimum=0, scale=1)
                    refresh = gr.Button("Refresh", scale=1)
                with gr.Row(visible=False) as assign_row:
                    assignee = gr.Dropdown(label="Assign to", choices=[], scale=3)
                    do_assign = gr.Button("Assign", scale=1)
                with gr.Row(visible=False) as work_row:
                    work = gr.Button("Work this case", variant="primary")
                with gr.Row() as close_row:
                    close_as = gr.Radio(
                        ["resolved", "rejected"], value="resolved", label="Close as", scale=1
                    )
                    resolution = gr.Textbox(label="What happened", scale=3)
                    close_case = gr.Button("Close", scale=1)
                cases_message = gr.Markdown("")

            with gr.Accordion("Approvals", open=False, visible=False) as approvals_panel:
                approvals = gr.Dataframe(
                    headers=APPROVAL_COLUMNS, interactive=False, wrap=True, label=None
                )
                with gr.Row():
                    approval_id = gr.Number(label="Request", precision=0, minimum=0, scale=1)
                    approve = gr.Button("Approve", variant="primary", scale=1)
                    reject = gr.Button("Reject", scale=1)
                approvals_message = gr.Markdown("")

            # Loaded on demand rather than with every refresh: fetching it on every page
            # event would be a request per keystroke elsewhere on the page. Pressing Show
            # with the box empty reads this session, so the panel is usable by someone who
            # has never seen a session id.
            with gr.Accordion("Audit trail", open=False, visible=False) as audit_panel:
                with gr.Row():
                    audit_session = gr.Textbox(
                        label="Session", placeholder="blank = this session", scale=3
                    )
                    show_audit = gr.Button("Show", scale=1)
                audit = gr.Dataframe(
                    headers=AUDIT_COLUMNS, interactive=False, wrap=True, label=None
                )
                audit_message = gr.Markdown("")

        # ---- handlers ---------------------------------------------------
        #
        # Each returns the same tuple of updates, so that one failure path cannot leave
        # the page half switched: signed out with a conversation still on screen, or
        # signed in with no account line.

        def enter(visitor: Visitor) -> tuple[Any, ...]:
            """What the page shows when it opens.

            With a pool configured this is the sign-in form. Without one the service is
            in its unauthenticated mode, so the page reads the identity it will act as
            and goes straight to the conversation.
            """
            if cognito is not None:
                return _signed_out("")
            try:
                account = api.me(None)
            except ApiError as exc:
                return _signed_out(f"The service is not ready: {exc}")
            ready = Visitor(session_id=uuid.uuid4().hex, account=account)
            return _signed_in(ready, "")

        def do_sign_in(name: str, secret: str, visitor: Visitor) -> tuple[Any, ...]:
            if cognito is None:  # pragma: no cover - the button is hidden in this mode
                return _signed_out("This service is not configured for sign-in.")
            try:
                signed_in = authenticate(cognito, api, name, secret)
            except SignInError as exc:
                return _signed_out(f"**{exc}**")
            except ForbiddenError as exc:
                # Authenticated, and no account here. Named rather than reported as a
                # sign-in failure, because the password was right and telling someone
                # their password is wrong when it is not sends them to the wrong place.
                return _signed_out(f"**Signed in, but this account has no access.** {exc}")
            except ApiError as exc:
                return _signed_out(f"**Signed in, but the service is not answering.** {exc}")
            return _signed_in(signed_in, "")

        def do_sign_out(visitor: Visitor) -> tuple[Any, ...]:
            """Drop the tokens. The pool is not told: this is one browser session ending,
            and a global sign-out would end the others."""
            return _signed_out(SIGNED_OUT)

        def respond(
            message: str, history: list[dict[str, Any]], visitor: Visitor
        ) -> Iterator[tuple[Any, ...]]:
            """One turn, rendered while it happens (M7.1).

            A generator rather than a function. The turn takes as long as it took before;
            what changes is that the model call, each tool and each guard decision appear
            as they finish, instead of a blank panel and then everything at once.
            """
            if not visitor.signed_in:
                yield _signed_out("**Sign in first.**")
                return
            if not message.strip():
                yield _stay(visitor, history, "", Situation(label="answered"))
                return

            asked = [*history, {"role": "user", "content": message}]
            steps: list[str] = []
            # Before anything is sent. The first model call is the longest silence in a
            # turn, so the message has to be on the page before it starts.
            yield _stay(visitor, [*asked, _progress(steps)], "", WORKING)

            try:
                visitor, events = retrying(
                    visitor,
                    cognito,
                    lambda token: api.chat_stream(token, visitor.session_id, message),
                )
            except UnauthenticatedError as exc:
                yield _signed_out(render(situation_of_failure(exc)))
                return
            except ApiError as exc:
                yield _stay(visitor, history, "", situation_of_failure(exc))
                return

            reply: Reply | None = None
            try:
                for event in events:
                    if event.reply is not None:
                        reply = event.reply
                        break
                    steps.append(_describe(event))
                    yield _stay(visitor, [*asked, _progress(steps)], "", WORKING)
            except UnauthenticatedError as exc:
                # Mid-turn, and the token is gone. The same answer as before the turn:
                # back to the form with the conversation cleared.
                yield _signed_out(render(situation_of_failure(exc)))
                return
            except ApiError as exc:
                # The message stays on the page: it was sent, the turn ran, and pretending
                # otherwise would hide a write that may have happened.
                yield _stay(visitor, [*asked, _progress(steps)], "", situation_of_failure(exc))
                return

            if reply is None:
                yield _stay(
                    visitor,
                    [*asked, _progress(steps)],
                    "",
                    Situation(
                        label="✗ the turn ended without an answer",
                        advice="Try again, or start a new session.",
                    ),
                )
                return

            situation = situation_of(reply)
            if reply.tool_calls:
                situation = replace(
                    situation,
                    label=f"{situation.label} · tools called: {', '.join(reply.tool_calls)}",
                )
            yield _stay(
                visitor,
                [*asked, {"role": "assistant", "content": reply.reply}],
                "",
                situation,
            )

        def start_over(visitor: Visitor) -> tuple[Any, ...]:
            """A new conversation under the same account.

            A new session id rather than a reset of the old one: the account is unchanged,
            and the previous session's audit trail is a record, not scratch space.
            """
            if not visitor.signed_in:
                return _signed_out("**Sign in first.**")
            fresh = replace(visitor, session_id=uuid.uuid4().hex)
            return _stay(fresh, [], "", Situation(label="new session"))

        # ---- the case work (M7.3) ---------------------------------------
        #
        # Every one of these is a call the service authorizes on its own. What the page
        # adds is knowing which controls to offer and reporting what came back.

        def with_cases(visitor: Visitor) -> tuple[Visitor, Situation | None]:
            """Read what this account may see. Returns the visitor and any failure.

            A failure is returned rather than raised because it must not stop the page
            from rendering: a supervisor whose approvals query fails still has a
            conversation, and an empty table that means "the query failed" would read as
            "there are no cases".
            """
            if not visitor.signed_in:
                return visitor, None
            try:
                if visitor.may(CASES):
                    visitor, tickets = retrying(visitor, cognito, lambda token: api.tickets(token))
                    visitor = replace(visitor, cases=tickets)
                if visitor.may(ASSIGN):
                    visitor, people = retrying(
                        visitor, cognito, lambda token: api.assignable(token)
                    )
                    visitor = replace(visitor, assignable=people)
                if visitor.may(DECIDE):
                    visitor, waiting = retrying(
                        visitor, cognito, lambda token: api.approvals(token)
                    )
                    visitor = replace(visitor, approvals=waiting)
            except ApiError as exc:
                return visitor, situation_of_failure(exc)
            return visitor, None

        def refreshed(
            visitor: Visitor, history: list[dict[str, Any]], note: str
        ) -> tuple[Any, ...]:
            """Re-read the case work and render, with a note about what just happened."""
            visitor, failure = with_cases(visitor)
            if failure is not None and failure.signed_out:
                return _signed_out(render(failure))
            trouble = render(failure) if failure is not None else ""
            return _stay(
                visitor,
                history,
                "",
                Situation(label="answered"),
                cases_note=note or trouble,
            )

        def do_refresh(visitor: Visitor, history: list[dict[str, Any]]) -> tuple[Any, ...]:
            if not visitor.signed_in:
                return _signed_out("**Sign in first.**")
            return refreshed(visitor, history, "")

        def act_on_case(
            visitor: Visitor,
            history: list[dict[str, Any]],
            call: Any,
            said: Any,
        ) -> tuple[Any, ...]:
            """Run one case action and re-read the list.

            Re-reading rather than patching the row: the service decides what a case looks
            like after an action, and a page that guessed would show a state the database
            does not have.
            """
            if not visitor.signed_in:
                return _signed_out("**Sign in first.**")
            try:
                visitor, outcome = retrying(visitor, cognito, call)
            except UnauthenticatedError as exc:
                return _signed_out(render(situation_of_failure(exc)))
            except ApiError as exc:
                return refreshed(visitor, history, render(situation_of_failure(exc)))
            return refreshed(visitor, history, said(outcome))

        def do_assign_click(
            visitor: Visitor, history: list[dict[str, Any]], number: float | None, subject: str
        ) -> tuple[Any, ...]:
            case = int(number or 0)
            if not case or not subject:
                return refreshed(visitor, history, "**Pick a case and somebody to give it to.**")
            return act_on_case(
                visitor,
                history,
                lambda token: api.assign(token, case, subject),
                lambda ticket: f"**Case {ticket.id} is with {ticket.assignee}.**",
            )

        def do_close(
            visitor: Visitor,
            history: list[dict[str, Any]],
            number: float | None,
            state: str,
            resolution_text: str,
        ) -> tuple[Any, ...]:
            case = int(number or 0)
            if not case or not resolution_text.strip():
                return refreshed(visitor, history, "**Pick a case and say what happened.**")
            return act_on_case(
                visitor,
                history,
                lambda token: api.close(token, case, state, resolution_text.strip()),
                lambda ticket: (
                    f"**Case {ticket.id} is {ticket.state}.** The customer's session runs again."
                ),
            )

        def do_work(visitor: Visitor, number: float | None) -> tuple[Any, ...]:
            """Open the session in which this account works a case.

            Their own session, not the customer's: the customer's conversation belongs to
            the customer, and M3's ownership check enforces that against the person
            helping them too. The conversation on screen is cleared because it is a
            different conversation.
            """
            case = int(number or 0)
            if not visitor.signed_in:
                return _signed_out("**Sign in first.**")
            if not case:
                return refreshed(visitor, [], "**Pick a case to work.**")
            try:
                visitor, opened = retrying(visitor, cognito, lambda token: api.work(token, case))
            except UnauthenticatedError as exc:
                return _signed_out(render(situation_of_failure(exc)))
            except ApiError as exc:
                return refreshed(visitor, [], render(situation_of_failure(exc)))
            visitor = replace(visitor, session_id=opened.session_id, working=opened.ticket_id)
            return refreshed(
                visitor,
                [],
                f"**Working case {opened.ticket_id} for {opened.user_id}**, in a session of "
                "your own. What you do here is recorded against your account.",
            )

        def do_decide(
            visitor: Visitor,
            history: list[dict[str, Any]],
            number: float | None,
            approve_it: bool,
        ) -> tuple[Any, ...]:
            request = int(number or 0)
            if not visitor.signed_in:
                return _signed_out("**Sign in first.**")
            if not request:
                return _approvals_note(visitor, history, "**Pick a request to decide.**")
            try:
                visitor, state = retrying(
                    visitor, cognito, lambda token: api.decide(token, request, approve_it)
                )
            except UnauthenticatedError as exc:
                return _signed_out(render(situation_of_failure(exc)))
            except ApiError as exc:
                # The refusal that matters is 403 "Nobody may decide their own escalation",
                # which is the separation of duties this project keeps claiming.
                return _approvals_note(visitor, history, render(situation_of_failure(exc)))
            return _approvals_note(visitor, history, f"**Request {request} is {state}.**")

        def do_show_audit(
            visitor: Visitor, history: list[dict[str, Any]], session_id: str
        ) -> tuple[Any, ...]:
            """Fetch one session's audit trail.

            It answers with the whole page tuple like every other handler. The first
            version wrote only its own two components, which looked tidier and was
            wrong: a 401 here would have left the page showing a signed-in account while
            the token was gone, and the audit panel is precisely where that would matter.

            An empty trail is reported as empty rather than left as a blank table. For a
            session where nothing was refused, empty is the correct answer, and it must
            not look identical to a request that failed.
            """
            if not visitor.signed_in:
                return _signed_out("**Sign in first.**")
            wanted = session_to_audit(session_id, visitor)
            if not wanted:
                return _audit_note(visitor, history, [], "**Name a session to look at.**")
            try:
                visitor, records = retrying(
                    visitor, cognito, lambda token: api.audit(token, wanted)
                )
            except UnauthenticatedError as exc:
                return _signed_out(render(situation_of_failure(exc)))
            except ApiError as exc:
                # 403 when the account lacks audit:read. The panel is hidden in that
                # case, so this is the path where the permission was revoked between the
                # page loading and the button being pressed - which is the behaviour
                # roles-from-the-database exists to give.
                return _audit_note(visitor, history, [], render(situation_of_failure(exc)))
            if not records:
                return _audit_note(visitor, history, [], f"**No records for `{wanted}`.**")
            return _audit_note(
                visitor,
                history,
                audit_table(records),
                f"**{len(records)} record(s) for `{wanted}`.**",
            )

        def _audit_note(
            visitor: Visitor,
            history: list[dict[str, Any]],
            rows: list[list[str]],
            note: str,
        ) -> tuple[Any, ...]:
            return _stay(
                visitor,
                history,
                "",
                situation_of(Reply(reply="", status="answered")),
                audit_rows=rows,
                audit_note=note,
            )

        def _approvals_note(
            visitor: Visitor, history: list[dict[str, Any]], note: str
        ) -> tuple[Any, ...]:
            visitor, failure = with_cases(visitor)
            if failure is not None and failure.signed_out:
                return _signed_out(render(failure))
            return _stay(
                visitor,
                history,
                "",
                Situation(label="answered"),
                approvals_note=note,
            )

        # ---- shapes of the update tuple ---------------------------------
        #
        # The box and the Send button are part of the state, not decoration around it
        # (M7.2). Three states cannot be advanced by typing, and a page that leaves the
        # box open in those is offering an action whose only possible answer is the
        # sentence already on screen.
        #
        # From M7.3 the case sections are in here too, so one failure path cannot leave
        # the page half switched: signed out with somebody's cases still on screen, or a
        # supervisor's approval queue visible to the customer who signed in after them.

        def _hidden() -> tuple[Any, ...]:
            """The case sections, closed. What a signed-out page and a customer share."""
            return (
                gr.update(visible=False),
                [],
                gr.update(visible=False),
                gr.update(choices=[], value=None),
                gr.update(visible=False),
                gr.update(visible=False),
                "",
                gr.update(visible=False),
                [],
                "",
                gr.update(visible=False),
                [],
                "",
            )

        def _sections(
            visitor: Visitor,
            cases_note: str,
            approvals_note: str,
            audit_rows: list[list[str]] | None = None,
            audit_note: str = "",
        ) -> tuple[Any, ...]:
            """The case sections for one account.

            Visible exactly when the permission is held. Hiding is not enforcement - every
            route refuses a caller who lacks the permission, and tests/test_auth.py asserts
            that route by route - but a control that will be refused is not worth offering.
            """
            return (
                gr.update(visible=visitor.may(CASES)),
                cases_table(visitor.cases),
                gr.update(visible=visitor.may(ASSIGN)),
                gr.update(choices=assignee_choices(visitor.assignable)),
                gr.update(visible=visitor.may(WORK)),
                gr.update(visible=visitor.may(CASES)),
                cases_note,
                gr.update(visible=visitor.may(DECIDE)),
                approvals_table(visitor.approvals),
                approvals_note,
                gr.update(visible=visitor.may(AUDIT)),
                audit_rows if audit_rows is not None else [],
                audit_note,
            )

        def _signed_out(message: str) -> tuple[Any, ...]:
            return (
                Visitor(),
                gr.update(visible=cognito is not None),
                gr.update(visible=False),
                message,
                "",
                [],
                gr.update(value="", interactive=True, placeholder=OPEN_PLACEHOLDER),
                gr.update(interactive=True),
                "",
                *_hidden(),
            )

        def _signed_in(visitor: Visitor, message: str) -> tuple[Any, ...]:
            assert visitor.account is not None
            return (
                visitor,
                gr.update(visible=False),
                gr.update(visible=True),
                message,
                describe(visitor.account),
                [],
                gr.update(value="", interactive=True, placeholder=OPEN_PLACEHOLDER),
                gr.update(interactive=True),
                "",
                *_sections(visitor, "", ""),
            )

        def _stay(
            visitor: Visitor,
            history: list[dict[str, Any]],
            box_value: str,
            situation: Situation,
            cases_note: str = "",
            approvals_note: str = "",
            audit_rows: list[list[str]] | None = None,
            audit_note: str = "",
        ) -> tuple[Any, ...]:
            assert visitor.account is not None
            return (
                visitor,
                gr.update(visible=False),
                gr.update(visible=True),
                "",
                describe(visitor.account, visitor.working),
                history,
                gr.update(
                    value=box_value,
                    interactive=situation.can_send,
                    placeholder=OPEN_PLACEHOLDER if situation.can_send else CLOSED_PLACEHOLDER,
                ),
                gr.update(interactive=situation.can_send),
                render(situation),
                *_sections(visitor, cases_note, approvals_note, audit_rows, audit_note),
            )

        outputs = [
            visitor_state,
            sign_in_panel,
            app_panel,
            sign_in_message,
            whoami,
            chatbot,
            box,
            send,
            status,
            cases_panel,
            cases,
            assign_row,
            assignee,
            work_row,
            close_row,
            cases_message,
            approvals_panel,
            approvals,
            approvals_message,
            audit_panel,
            audit,
            audit_message,
        ]

        demo.load(enter, visitor_state, outputs)
        # The password box is cleared by the same event that reads it, so a failed
        # sign-in does not leave it on screen for the next person at the machine.
        sign_in.click(do_sign_in, [username, password, visitor_state], outputs).then(
            lambda: "", None, password
        )
        password.submit(do_sign_in, [username, password, visitor_state], outputs).then(
            lambda: "", None, password
        )
        sign_out.click(do_sign_out, visitor_state, outputs)
        send.click(respond, [box, chatbot, visitor_state], outputs)
        box.submit(respond, [box, chatbot, visitor_state], outputs)
        reset.click(start_over, visitor_state, outputs)

        refresh.click(do_refresh, [visitor_state, chatbot], outputs)
        do_assign.click(do_assign_click, [visitor_state, chatbot, case_id, assignee], outputs)
        close_case.click(do_close, [visitor_state, chatbot, case_id, close_as, resolution], outputs)
        work.click(do_work, [visitor_state, case_id], outputs)
        approve.click(
            lambda v, h, n: do_decide(v, h, n, True), [visitor_state, chatbot, approval_id], outputs
        )
        reject.click(
            lambda v, h, n: do_decide(v, h, n, False),
            [visitor_state, chatbot, approval_id],
            outputs,
        )
        show_audit.click(do_show_audit, [visitor_state, chatbot, audit_session], outputs)
        audit_session.submit(do_show_audit, [visitor_state, chatbot, audit_session], outputs)

    assert isinstance(demo, gr.Blocks)
    return demo


def main() -> None:
    """Binds to localhost by default; no share link, no public registration.

    The address is configurable because inside a container 127.0.0.1 is unreachable from
    outside it, and a demo has to be reachable to be a demo. The default is unchanged, so
    running this on a developer's machine still listens on the loopback address only.
    Exposing it takes an explicit `RSA_UI_HOST=0.0.0.0`, which is a decision someone makes
    rather than one they inherit.
    """
    build_demo().launch(
        server_name=os.getenv("RSA_UI_HOST", "127.0.0.1"),
        server_port=int(os.getenv("RSA_UI_PORT", "7860")),
        share=False,
    )


if __name__ == "__main__":
    main()
