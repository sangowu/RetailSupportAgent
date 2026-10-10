"""Single LangGraph agent: the minimal business loop.

The flow matches DEVELOPMENT_PLAN.md M0.3:
    look up order -> ask for missing details -> apply policy -> explicit confirmation
    -> validate -> execute -> report the actual result

The model can only propose a tool call. `guardrails.WriteGuard` decides whether it
runs, and a write needs a confirmation bound to that exact argument set. A model
claiming "the user already confirmed" does not pass.

Answers must reflect what the tools actually returned: an accepted return request is
not a refund that has landed. This is stated in the system prompt and backed by the
DB component of the benchmark reward.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from retail_support_agent.auth import Permission
from retail_support_agent.domain import HANDOFF_TOOLS, WRITE_TOOLS, RetailDomain, ToolResult
from retail_support_agent.guardrails import (
    Decision,
    ProposedCall,
    SessionState,
    WriteGuard,
)
from retail_support_agent.handoff import hand_off, handed_off_reply, waiting_reply
from retail_support_agent.scope import AccessScope, audited, identifiers_in
from retail_support_agent.store import APPROVAL_TTL_SECONDS, Store

# This prompt is for the application demo only.
# The standard τ²-bench run uses upstream's own identification protocol and agent
# prompt. The two are recorded separately, and the fixed identity below must never
# be used to change standard scoring conditions (AGENTS.md, DEVELOPMENT_PLAN.md).
SYSTEM_PROMPT_TEMPLATE = """You are a retail customer-support agent.

The application layer already authenticated the user at login. The current user id
is {user_id}. Do not ask for an email or a name and zip code to verify identity, and
do not call find_user_id_* to "confirm" it. When you need the user's profile, call
get_user_details with {user_id}.

Apart from identification, follow this domain policy exactly as written. Do not relax it:

{policy}

Additional hard requirements:

1. Only act on orders belonging to {user_id}. Refuse anything touching another user
   and say why.
2. Ask for missing details instead of guessing an order id, item, or quantity.
3. Confirmation has two stages and both must happen. First explain the operation to
   the user as the policy requires and get their agreement; this stage is required by
   the policy and cannot be skipped. Once they agree, issue the tool call with
   complete arguments. The system then holds the call and shows the full arguments to
   the user for a final confirmation before anything runs. You may not confirm on the
   user's behalf, and you must not claim an action is done before it is.
4. Answers must match the state the tools returned. An accepted return or exchange is
   not a completed refund. State where the request actually stands.
5. When you cannot handle something, say why or hand off to a human. Never invent a
   completed action.
"""


class AgentState(TypedDict, total=False):
    """LangGraph state. `messages` accumulates through add_messages."""

    messages: Annotated[list[dict[str, Any]], add_messages]
    status: str
    stop_reason: str


@dataclass
class TurnResult:
    """Result of one turn, consumed by the API and the UI."""

    reply: str
    # M4 adds three: the turn produced a ticket, the session is suspended behind one, or
    # the action is waiting for a supervisor's decision. They are distinct states rather
    # than shades of "answered", because a caller acts differently on each.
    status: Literal[
        "answered",
        "needs_confirmation",
        "needs_approval",
        "denied",
        "stopped",
        "handed_off",
        "waiting_for_human",
    ]
    tool_calls: list[str] = field(default_factory=list)
    pending_confirmation: str | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class TurnEvent:
    """One step of a turn, as it finishes (M7.1).

    A turn is not one thing that takes eighteen seconds. It is a model call, a tool, a
    guard decision and another model call, and every one of those is worth seeing. The
    final event carries the whole result, so this is a view of a turn rather than a
    second account of it.
    """

    kind: Literal["thinking", "tool", "held", "approval", "refused", "reply"]
    tool: str | None = None
    detail: str = ""
    # Set on "reply" and nowhere else.
    result: TurnResult | None = None


# Statuses whose reply the guard wrote, not the model. Their text is the proposed call
# quoted back, so the output-attribution check does not apply to it: see `_check_output`.
GUARD_AUTHORED = frozenset({"needs_confirmation", "needs_approval", "denied"})


def _pending_tool(session: SessionState) -> str | None:
    """Which call the guard is holding, if any."""
    return session.pending.name if session.pending is not None else None


@dataclass
class RecoveryReport:
    """What a recovery restored. Returned so a caller can log it and tests can assert on
    it, instead of inferring recovery from side effects."""

    recovered: bool
    messages: int = 0
    operations_replayed: int = 0
    pending_confirmation: bool = False


ChatFn = Callable[[list[dict[str, Any]], list[dict[str, Any]]], Any]
"""(messages, tools) -> an OpenAI-shaped ChatCompletion response. Lets tests use a fake model."""


def _tool_calls_of(message: Any) -> list[Any]:
    return list(getattr(message, "tool_calls", None) or [])


def _parse_arguments(raw: str | None) -> dict[str, Any] | None:
    """Model arguments may not be valid JSON. That is a business failure, reported as one."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class RetailAgent:
    """One instance per session, with its own domain environment and state."""

    def __init__(
        self,
        chat_fn: ChatFn,
        domain: RetailDomain,
        session: SessionState,
        store: Store | None = None,
        session_id: str | None = None,
        approval_ttl_seconds: float = APPROVAL_TTL_SECONDS,
    ) -> None:
        self._chat = chat_fn
        self._domain = domain
        self._session = session
        # With store=None every persistence call is a no-op. The database is a
        # capability, not a precondition: the service and the evaluation loop must
        # run without it, and the evaluation loop has no PostgreSQL.
        self._store = store
        self._session_id = session_id
        self._confirmation_id: int | None = None
        # Who is driving the session, and the ticket they are working (M4). Both stay
        # None for a customer's own session and for the demo path.
        self._subject: str | None = None
        self._ticket_id: int | None = None
        self._approval_ttl = approval_ttl_seconds
        # Requests already said to have run out of time, so the session is told once
        # rather than at the top of every turn for the next hour.
        self._announced_timeouts: set[int] = set()
        if self._store is not None and self._session_id is not None:
            self._store.ensure_session(self._session_id, session.user_id)
        self._guard = WriteGuard(domain.tool_names(), domain.order_owner, domain.order_status)
        # Tools actually executed during this turn. This must accumulate across the
        # whole turn, not just the last step: a turn is usually think (request tool)
        # -> act (execute) -> think (answer), and the last step has no tool calls.
        # Building a fresh empty list there discarded everything executed earlier, so
        # read-only queries always reported "no tools called" while they had in fact
        # queried the database.
        self._turn_tools: list[str] = []
        self._tools = domain.tool_schemas()
        self._graph = self._build_graph()
        self._messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT_TEMPLATE.format(
                    user_id=session.user_id, policy=domain.policy
                ),
            }
        ]
        self._last: TurnResult | None = None

    def apply_context(
        self,
        permissions: frozenset[Permission],
        subject: str | None = None,
        ticket_id: int | None = None,
        scope: AccessScope | None = None,
    ) -> None:
        """Set who is driving this session and what they may do, before the turn runs.

        Called on every request rather than only at construction: an agent cached in this
        process would otherwise keep the permissions it was built with, and a role
        revoked (M3) or an assignment released (M4) would keep working until the process
        restarted.

        The approvals are read here too, so a decision that expired between two turns
        stops authorizing the action without anything having to expire it in memory.
        Recovery gets this for free: a rebuilt session reads the same rows.
        """
        self._session.permissions = permissions
        # The access scope is refreshed here for the same reason as the permissions: an
        # agent cached in this process would otherwise keep the slice it was built with.
        # None means "leave it alone", which is what the callers that predate the scope
        # want; the API always passes one.
        if scope is not None:
            self._session.scope = scope
        self._subject = subject
        self._ticket_id = ticket_id
        self._session.acting_for_other = ticket_id is not None
        if self._session.acting_for_other and self._store is not None and self._session_id:
            self._session.approved_fingerprints = self._store.approved_fingerprints(
                self._session_id
            )
            self._note_timed_out_requests()
            self._note_approved_but_not_run()

    def _note_timed_out_requests(self) -> None:
        """Tell the session about a request whose window closed with no decision.

        Nothing happens when an approval expires: the row stays pending and the deadline
        simply passes. Until this, the session went on as though the request were still
        waiting - asked for an update, it answered that the cancellation was pending a
        supervisor's approval, about a request no supervisor could see or decide - and
        the work stopped there for good, with neither side told.

        Said once per request, and in the conversation rather than as a state flag,
        because the next thing to do is a judgement: raise it again, or tell the customer
        it did not happen. Recovery re-derives it from the same rows, so a rebuilt
        session is not silent about it either.
        """
        if self._store is None or self._session_id is None:
            return
        for row in self._store.timed_out_approvals(self._session_id):
            request_id = int(row.id)
            if request_id in self._announced_timeouts:
                continue
            self._announced_timeouts.add(request_id)
            arguments = ", ".join(f"{k}={v!r}" for k, v in sorted(dict(row.arguments).items()))
            self._messages.append(
                {
                    "role": "system",
                    "content": (
                        f"The request for approval to run {row.tool_name} ({arguments}) ran "
                        "out of time and nobody decided it. Nothing is waiting on it now, "
                        "and it cannot be approved any more. If the action is still needed, "
                        "propose it again, which raises a new request; otherwise say plainly "
                        "that it did not happen."
                    ),
                }
            )

    def _note_approved_but_not_run(self) -> None:
        """Tell the session about a decision that was granted and never acted on.

        Approving does not run anything. It records that one argument set may run, and
        the action still has to be proposed again - so between the decision and the next
        tool call there is a gap in which the work is authorized and not done.

        Walked in a browser, that gap produced the worst answer this project has
        recorded: told the request was approved, the session reported the order
        cancelled and a refund on its way. Nothing had run. `operations` was empty, the
        audit trail held no `write_executed`, and the customer would have been told their
        order was cancelled when it was not.

        Unlike a request that ran out of time, this is a standing condition rather than
        an event: it holds until the action runs or the decision expires, so it is said
        on every turn it is still true.
        """
        if self._store is None or self._session_id is None:
            return
        approved = self._store.approved_requests(self._session_id)
        if not approved:
            return
        done = {
            ProposedCall(op.tool_name, dict(op.arguments)).fingerprint
            for op in self._store.operations(self._session_id)
            if op.status == "succeeded"
        }
        for row in approved:
            if row.fingerprint in done:
                continue
            arguments = ", ".join(f"{k}={v!r}" for k, v in sorted(dict(row.arguments).items()))
            self._messages.append(
                {
                    "role": "system",
                    "content": (
                        f"A supervisor approved running {row.tool_name} ({arguments}), and it "
                        "has not run yet. Approval authorizes the action; it does not perform "
                        "it. If it is still what the customer asked for, call the tool now "
                        "with exactly those arguments. Do not say it is done until the tool "
                        "has returned a result."
                    ),
                }
            )

    # ---- graph wiring ---------------------------------------------------

    def _build_graph(self) -> Any:
        graph: StateGraph[AgentState] = StateGraph(AgentState)
        graph.add_node("think", self._node_think)
        graph.add_node("act", self._node_act)
        graph.set_entry_point("think")
        graph.add_conditional_edges(
            "think",
            self._route_after_think,
            {"act": "act", "done": END},
        )
        # Only a real execution returns to the model to explain the result. A pending
        # confirmation or a stop must end the turn immediately, otherwise the model is
        # called again and overwrites the "waiting for your confirmation" state.
        graph.add_conditional_edges(
            "act",
            self._route_after_act,
            {"think": "think", "done": END},
        )
        return graph.compile()

    def _route_after_think(self, state: AgentState) -> str:
        return "act" if state.get("status") == "tool_requested" else "done"

    def _route_after_act(self, state: AgentState) -> str:
        return "think" if state.get("status") == "tool_executed" else "done"

    # ---- nodes ----------------------------------------------------------

    def _node_think(self, state: AgentState) -> AgentState:
        """Call the model. Stop conditions are checked first, so no request is billed
        once a limit is reached."""
        stop = self._session.stop_reason()
        if stop is not None:
            self._last = TurnResult(
                reply=f"This session has stopped: {stop} Start a new one or ask for a human agent.",
                status="stopped",
                tool_calls=list(self._turn_tools),
                stop_reason=stop,
            )
            return {"status": "stopped", "stop_reason": stop}

        self._session.steps += 1
        response = self._chat(self._messages, self._tools)
        message = response.choices[0].message
        calls = _tool_calls_of(message)

        self._messages.append(_assistant_message(message, calls))
        if not calls:
            self._last = TurnResult(
                reply=message.content or "",
                status="answered",
                tool_calls=list(self._turn_tools),
            )
            return {"status": "answered"}
        return {"status": "tool_requested"}

    def _node_act(self, state: AgentState) -> AgentState:
        """Judge and run each tool call the model proposed."""
        pending_calls = _tool_calls_of_last_assistant(self._messages)

        for call in pending_calls:
            arguments = _parse_arguments(call["function"].get("arguments"))
            if arguments is None:
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": "Arguments are not a valid JSON object. Nothing ran. "
                        "Fix them and retry.",
                    }
                )
                continue

            proposed = ProposedCall(call["function"]["name"], arguments)
            verdict = self._guard.review(proposed, self._session)

            if verdict.decision is Decision.NEEDS_CONFIRMATION:
                self._session.pending = proposed
                self._record_confirmation_request(proposed)
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": "Not executed: this write action needs the user to confirm "
                        "these specific arguments.",
                    }
                )
                self._last = TurnResult(
                    reply=verdict.reason,
                    status="needs_confirmation",
                    tool_calls=list(self._turn_tools),
                    pending_confirmation=proposed.describe(),
                )
                return {"status": "needs_confirmation"}

            if verdict.decision is Decision.NEEDS_APPROVAL:
                # The request is recorded so a supervisor has something to decide. The
                # turn ends here: continuing would let the model answer as though the
                # action had run.
                if self._store is not None and self._session_id is not None:
                    self._store.open_approval(
                        self._session_id,
                        proposed,
                        requested_by=self._subject or "",
                        ticket_id=self._ticket_id,
                        ttl_seconds=self._approval_ttl,
                    )
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": "Not executed: this action is waiting for a supervisor's "
                        "approval.",
                    }
                )
                self._last = TurnResult(
                    reply=verdict.reason,
                    status="needs_approval",
                    tool_calls=list(self._turn_tools),
                    pending_confirmation=proposed.describe(),
                )
                return {"status": "needs_approval"}

            if not verdict.allowed:
                self._messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": verdict.reason}
                )
                self._last = TurnResult(
                    reply=verdict.reason, status="denied", tool_calls=list(self._turn_tools)
                )
                continue

            self._session.tool_calls += 1
            result = self._execute(proposed)
            self._turn_tools.append(proposed.name)
            self._messages.append(result.as_tool_message(call["id"]))

            if proposed.name in HANDOFF_TOOLS:
                self._last = self._hand_off(proposed)
                return {"status": "answered"}

        return {"status": "tool_executed"}

    # ---- public interface -----------------------------------------------

    def _hand_off(self, call: ProposedCall) -> TurnResult:
        """Turn a transfer into a ticket a human owns (M4).

        Without a database this is what it was before: a sentence. With one, the case
        becomes a record with a requester and an outcome, and the session is suspended
        until a human closes it - otherwise the model would keep working on a case it
        had just escalated.
        """
        summary = str(call.arguments.get("summary", ""))
        ticket = hand_off(
            self._store, self._session_id, self._session.user_id, summary, self._subject
        )
        return TurnResult(
            reply=handed_off_reply(ticket),
            status="answered" if ticket is None else "handed_off",
            tool_calls=list(self._turn_tools),
        )

    def _waiting_for_human(self) -> TurnResult | None:
        """The reply for a session a human is holding, or None if it may proceed.

        Checked before anything else in a turn, so a suspended session bills no model
        call and runs no tool.
        """
        if self._store is None or self._session_id is None:
            return None
        ticket = self._store.open_ticket_for(self._session_id)
        if ticket is None or self._ticket_id is not None:
            # An agent working the ticket is not blocked by it; the customer is.
            return None
        return TurnResult(reply=waiting_reply(ticket), status="waiting_for_human", tool_calls=[])

    # ---- watching a turn happen (M7.1) ----------------------------------

    def stream(self, user_message: str) -> Iterator[TurnEvent]:
        """Handle one user message, reporting each step as it finishes.

        The same turn `send` runs, driven a node at a time instead of in one call, so a
        caller can show the work while it happens rather than a blank panel until it is
        over. The last event carries the whole `TurnResult`, so a caller that ignores
        every other event behaves exactly like a caller of `send`.

        `send` is deliberately not written in terms of this. It is on the benchmark's
        import path, and putting the measured path through new code would owe a full
        re-run to show a baseline did not move. The two are held together by a test that
        runs one conversation both ways and compares the results and the database
        (M7.1 section 1.4).

        Events are derived from what the nodes left behind rather than emitted by them,
        for the same reason: the nodes are the measured path and this adds nothing to it.
        """
        # One turn is what the session's budget is spent on. Begun here and ended in
        # `finally`, so every exit from this route - including the short one below
        # and an exception out of the graph - stops the clock exactly once.
        self._session.turn_begins()
        try:
            waiting = self._waiting_for_human()
            if waiting is not None:
                self._journal("user", user_message, [])
                self._journal("assistant", waiting.reply, [])
                yield TurnEvent(kind="reply", result=waiting)
                return

            self._maybe_consume_confirmation(user_message)
            self._messages.append({"role": "user", "content": user_message})
            self._journal("user", user_message, [])
            self._last = None
            self._turn_tools = []
            # The turn may say back what it reads from here on, and what the user just typed.
            # Cleared rather than accumulated: see AccessScope.withhold.
            self._session.disclosed_ids = set(identifiers_in(user_message))

            # Before the first model call, not after it. The point of this route is that
            # something appears immediately, and the first model call is the longest silence
            # in the turn.
            yield TurnEvent(kind="thinking")

            ran = 0
            for chunk in self._graph.stream({"messages": []}):
                node = next(iter(chunk), "")
                if node != "act":
                    continue
                yield from self._events_after_act(ran)
                ran = len(self._turn_tools)
                # The model is about to be asked to explain what the tools returned. Only
                # when the turn continues: a held write ends it, and announcing a call that
                # will not happen would be a lie about the state.
                if chunk[node].get("status") == "tool_executed":
                    yield TurnEvent(kind="thinking")

            result = self._last or TurnResult(
                reply="(the model returned no answer)",
                status="answered",
                tool_calls=list(self._turn_tools),
            )
            result = self._check_output(result)
            self._journal("assistant", result.reply, result.tool_calls)
            self._record_access_decisions()
            self._save_progress()
            yield TurnEvent(kind="reply", result=result)
        finally:
            self._session.turn_ends()

    def _events_after_act(self, ran: int) -> Iterator[TurnEvent]:
        """What the act node just did, read from what it left behind.

        Tools that ran are appended to `_turn_tools`, so anything past `ran` is new. A
        guard decision that ended the turn is in `_last`; a refusal that did not end it
        is there too, because the node records one before continuing to the next call.
        """
        for name in self._turn_tools[ran:]:
            yield TurnEvent(kind="tool", tool=name)

        if self._last is None:
            return
        if self._last.status == "needs_confirmation":
            yield TurnEvent(
                kind="held",
                tool=_pending_tool(self._session),
                detail=self._last.pending_confirmation or "",
            )
        elif self._last.status == "needs_approval":
            yield TurnEvent(
                kind="approval",
                tool=_pending_tool(self._session),
                detail=self._last.pending_confirmation or "",
            )
        elif self._last.status == "denied":
            yield TurnEvent(kind="refused", detail=self._last.reply)

    def send(self, user_message: str) -> TurnResult:
        """Handle one user message. Calls for one session must be serialized."""
        # One turn is what the session's budget is spent on. Begun here and ended in
        # `finally`, so every exit from this route - including the short one below
        # and an exception out of the graph - stops the clock exactly once.
        self._session.turn_begins()
        try:
            waiting = self._waiting_for_human()
            if waiting is not None:
                # The message is still journaled: what the customer said while waiting is
                # part of the case, and the human handling it needs to see it.
                self._journal("user", user_message, [])
                self._journal("assistant", waiting.reply, [])
                return waiting

            self._maybe_consume_confirmation(user_message)
            self._messages.append({"role": "user", "content": user_message})
            self._journal("user", user_message, [])
            self._last = None
            self._turn_tools = []
            # The turn may say back what it reads from here on, and what the user just typed.
            # Cleared rather than accumulated: see AccessScope.withhold.
            self._session.disclosed_ids = set(identifiers_in(user_message))
            self._graph.invoke({"messages": []})
            result = self._last or TurnResult(
                reply="(the model returned no answer)",
                status="answered",
                tool_calls=list(self._turn_tools),
            )
            result = self._check_output(result)
            self._journal("assistant", result.reply, result.tool_calls)
            self._record_access_decisions()
            self._save_progress()
            return result
        finally:
            self._session.turn_ends()

    # ---- recovery (M2) --------------------------------------------------

    def recover(self) -> RecoveryReport:
        """Rebuild this session from the database after a restart.

        Four things are restored, in this order:

        1. **The business state**, by replaying every succeeded write from `operations`
           onto the fresh domain. The retail tools are deterministic - no randomness and
           no clock - so replaying the same arguments in the same order onto the same
           initial data reproduces the same database. `db_hash` makes that checkable
           rather than assumed.
        2. **The conversation**, from `messages`. Only the visible turns were journaled,
           not the intermediate tool messages, so the model resumes with what the user
           saw rather than the full tool transcript. That is enough to continue the
           conversation and avoids storing a second copy of tool results that are
           already in `operations`.
        3. **The authorizations**: every confirmed fingerprint, so a write the user
           already approved is not held again, plus any confirmation still pending.
        4. **The stop conditions**: the counters and the session's age. Without them a
           restart would reset the limits, making a restart a way around them.

        Replay calls the domain directly and never goes through `execute_once`: a
        recovery that recorded new operations would turn rebuilding into new work, and
        the audit trail would show a write that no user authorized.
        """
        if self._store is None or self._session_id is None:
            return RecoveryReport(recovered=False)

        operations = self._store.operations(self._session_id)
        for op in operations:
            if op.status != "succeeded":
                continue
            self._domain.execute(op.tool_name, dict(op.arguments))

        stored = self._store.messages(self._session_id)
        for row in stored:
            if row.role in ("user", "assistant"):
                self._messages.append({"role": row.role, "content": row.content})

        self._session.confirmed_fingerprints |= self._store.confirmed_fingerprints(self._session_id)
        self._confirmation_id = self._store.last_confirmation_id(self._session_id)

        pending = self._store.pending_confirmation(self._session_id)
        if pending is not None:
            self._session.pending = ProposedCall(pending.tool_name, dict(pending.arguments))

        progress = self._store.progress(self._session_id)
        if progress is not None:
            self._session.steps = progress.steps
            self._session.tool_calls = progress.tool_calls
            # The time budget resumes where it was spent, not from when the session row
            # was created: the row's age is how long the case has been on somebody's
            # desk, which the budget is not about.
            self._session.worked_seconds = progress.worked_seconds

        return RecoveryReport(
            recovered=bool(stored or operations),
            messages=len(stored),
            operations_replayed=sum(1 for op in operations if op.status == "succeeded"),
            pending_confirmation=pending is not None,
        )

    def db_hash(self) -> str:
        """Fingerprint of the domain database. Used to check a recovery reproduced the
        state, rather than trusting that it did."""
        return self._domain.db_hash()

    # ---- persistence (no-ops when store is None) ------------------------

    def _journal(self, role: str, content: str, tool_calls: list[str]) -> None:
        """Write one turn into the messages table.

        The sequence number is the stored maximum plus one, and (session_id, seq) is
        unique, so a resent turn does not insert a second row. Only that duplicate is
        swallowed; any other exception propagates, because silently dropping a failed
        write would mean pretending an audit record exists.
        """
        if self._store is None or self._session_id is None:
            return
        seq = self._store.next_seq(self._session_id)
        self._store.append_message(self._session_id, seq, role, content, tool_calls)

    def _save_progress(self) -> None:
        """Persist the stop conditions so a restart resumes them.

        `elapsed()` rather than `worked_seconds`, because this runs inside the turn it is
        recording: the accumulator does not include the running turn, and elapsed does.
        """
        if self._store is None or self._session_id is None:
            return
        self._store.save_progress(
            self._session_id,
            self._session.steps,
            self._session.tool_calls,
            self._session.elapsed(),
        )

    def _record_confirmation_request(self, proposed: ProposedCall) -> None:
        if self._store is None or self._session_id is None:
            return
        self._store.open_confirmation(self._session_id, proposed)

    def _execute(self, proposed: ProposedCall) -> ToolResult:
        """Run one tool call. Writes go through the idempotent path.

        Reads are not recorded in operations: they have no side effect, repeating one
        changes nothing, and a row per read would bury the audit table in noise.
        """
        if self._store is None or self._session_id is None or proposed.name not in WRITE_TOOLS:
            return self._disclose(
                self._project(self._domain.execute(proposed.name, proposed.arguments))
            )

        outcome = self._store.execute_once(
            self._session_id,
            proposed,
            self._confirmation_id,
            lambda: self._domain.execute(proposed.name, proposed.arguments).content,
        )
        # A replay does not touch the domain environment. That second change is
        # exactly what idempotency exists to prevent.
        return self._disclose(
            self._project(ToolResult(name=proposed.name, content=outcome.result, ok=True))
        )

    def _check_output(self, result: TurnResult) -> TurnResult:
        """Hold the answer to what this turn actually read.

        The last gate in the turn. Everything before it decides what the model may
        *reach*; this decides what may leave, which is the only place a fabricated or
        carried-over identifier can still be caught. A withheld identifier is recorded in
        the slice audit, because an answer that was altered on the way out is exactly the
        event a reviewer needs to see.
        """
        scope = self._session.scope
        if not scope.check_output or not result.reply:
            return result
        if result.status in GUARD_AUTHORED:
            # Not the model's words. These replies are the guard quoting back the call it
            # is holding or refusing, and the arguments are the whole point of showing
            # them: a person is being asked to check, confirm or escalate exactly this.
            #
            # Withholding them made an escalation unreviewable by the person raising it.
            # In the deployed demo an agent was asked to escalate
            # `cancel_pending_order(order_id="[withheld: not read in this turn]")` while
            # the supervisor's queue and the database both held the real order. If the
            # model had named the wrong order, the one person positioned to notice was
            # the one shown a placeholder.
            #
            # Nothing can leak here that was not already proposed by the model, because
            # the text is built from the proposed call's own arguments.
            return result
        reply, withheld = scope.withhold(result.reply, frozenset(self._session.disclosed_ids))
        if not withheld:
            return result
        self._session.slice_audit.record(
            "(reply)",
            "withheld",
            f"identifiers not read in this turn: {', '.join(sorted(withheld))}",
            scope.source,
        )
        return replace(result, reply=reply)

    def _record_access_decisions(self) -> None:
        """Persist the turn's refusals and withholdings, and forget the rest.

        The guard records its verdicts in memory and knows nothing about a database:
        it is pure logic that a test can drive without one, and handing it a store would
        give that up. Draining here is what turns those verdicts into an audit trail that
        survives the process.
        """
        rows = self._session.slice_audit.drain()
        if self._store is None or self._session_id is None:
            return
        self._store.record_access_events(self._session_id, audited(rows))

    def _disclose(self, result: ToolResult) -> ToolResult:
        """Record the identifiers this tool result put in front of the model.

        Called on the projected result, not the raw one: a field the column slice hid is
        not something the turn was shown, so it must not license saying it back.
        """
        if result.ok:
            self._session.disclosed_ids |= identifiers_in(result.content)
        return result

    def _project(self, result: ToolResult) -> ToolResult:
        """Apply the column slice to what the model is about to read.

        Deliberately after `execute_once`, not inside it: the recorded operation result
        is what recovery replays, and storing a redacted copy would rebuild the session
        from data with holes in it. The redaction is for the model's view only.
        """
        if not result.ok or not self._session.scope.redacts:
            return result
        return ToolResult(
            name=result.name, ok=result.ok, content=self._session.scope.project(result.content)
        )

    def _maybe_consume_confirmation(self, user_message: str) -> None:
        """Record an affirmative reply as a confirmation of the pending argument set."""
        pending = self._session.pending
        if pending is None:
            return
        if _is_affirmative(user_message):
            self._session.confirm(pending)
            if self._store is not None and self._session_id is not None:
                # confirm returns None when the fingerprint does not match: the user
                # agreed to the arguments they were shown, so a changed argument set
                # has to be asked again.
                self._confirmation_id = self._store.confirm(self._session_id, pending.fingerprint)
        else:
            self._session.pending = None


AFFIRMATIVE = ("yes", "yep", "confirm", "confirmed", "go ahead", "please do", "ok", "okay", "sure")

NEGATIVE = ("no", "don't", "do not", "cancel that", "stop", "not yet", "hold off")


def _is_affirmative(text: str) -> bool:
    """Whether the reply is an explicit yes. Any negative word means it is not."""
    lowered = text.strip().lower()
    if any(word in lowered for word in NEGATIVE):
        return False
    return any(word in lowered for word in AFFIRMATIVE)


def _assistant_message(message: Any, calls: list[Any]) -> dict[str, Any]:
    """Convert a model response into a message we can send back, keeping tool_calls
    and reasoning_content.

    reasoning_content must be sent back. DeepSeek documents that when a request
    carries `tools`, "reasoning_content from previous turns should be returned to the
    API and will be concatenated into the context". Every turn here carries tools, so
    dropping it makes the model forget its own reasoning each turn. Nothing errors; it
    just gets worse. Requests without tools ignore the field, so it is attached only
    when present.
    """
    payload: dict[str, Any] = {"role": "assistant", "content": message.content}
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        payload["reasoning_content"] = reasoning
    if calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in calls
        ]
    return payload


def _tool_calls_of_last_assistant(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            calls: list[dict[str, Any]] = message.get("tool_calls") or []
            return calls
    return []
