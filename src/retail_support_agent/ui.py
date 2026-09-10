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
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

import gradio as gr

from retail_support_agent.client import (
    Account,
    Api,
    ApiError,
    Event,
    ForbiddenError,
    Reply,
    UnauthenticatedError,
)
from retail_support_agent.cognito import Cognito, SignInError, Tokens

STATUS_LABELS = {
    "answered": "answered",
    "needs_confirmation": "⚠ awaiting your confirmation",
    "denied": "✗ denied",
    "stopped": "■ session stopped",
}

# What the note area says while a turn is running. The status line is the only place the
# page reports state, so leaving the previous turn's status there would be a lie for as
# long as this one takes.
WORKING = "**Working…**"

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

    @property
    def signed_in(self) -> bool:
        return self.account is not None


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


def describe(account: Account) -> str:
    """The account line on the page.

    It names the retail identity and the roles because they are the evidence: they come
    from the database for the signed-in subject, and no field on this page can change
    them.
    """
    roles = ", ".join(account.roles) or "no roles"
    line = f"**Acting as** `{account.user_id}` · **roles:** {roles}"
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
                box = gr.Textbox(placeholder="e.g. show me my orders", scale=5, show_label=False)
                send = gr.Button("Send", variant="primary", scale=1)
            with gr.Row():
                reset = gr.Button("New session")
                sign_out = gr.Button("Sign out", visible=cognito is not None)

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
                yield _stay(visitor, history, "", "")
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
                yield _signed_out(f"**Signed out.** {exc}")
                return
            except ApiError as exc:
                yield _stay(visitor, history, "", f"**✗ {exc}**")
                return

            reply: Reply | None = None
            try:
                for event in events:
                    if event.reply is not None:
                        reply = event.reply
                        break
                    steps.append(_describe(event))
                    yield _stay(visitor, [*asked, _progress(steps)], "", WORKING)
            except ApiError as exc:
                # Mid-turn. The message stays on the page: it was sent, the turn ran, and
                # pretending otherwise would hide a write that may have happened.
                yield _stay(visitor, [*asked, _progress(steps)], "", f"**✗ {exc}**")
                return

            if reply is None:
                yield _stay(
                    visitor,
                    [*asked, _progress(steps)],
                    "",
                    "**✗ The turn ended without an answer.**",
                )
                return

            label = STATUS_LABELS.get(reply.status, reply.status)
            note = f"**Status: {label}**"
            if reply.tool_calls:
                note += f" · tools called: {', '.join(reply.tool_calls)}"
            if reply.stop_reason:
                note += f" · {reply.stop_reason}"
            yield _stay(
                visitor,
                [*asked, {"role": "assistant", "content": reply.reply}],
                "",
                note,
            )

        def start_over(visitor: Visitor) -> tuple[Any, ...]:
            """A new conversation under the same account.

            A new session id rather than a reset of the old one: the account is unchanged,
            and the previous session's audit trail is a record, not scratch space.
            """
            if not visitor.signed_in:
                return _signed_out("**Sign in first.**")
            fresh = replace(visitor, session_id=uuid.uuid4().hex)
            return _stay(fresh, [], "", "Started a new session.")

        # ---- shapes of the update tuple ---------------------------------

        def _signed_out(message: str) -> tuple[Any, ...]:
            return (
                Visitor(),
                gr.update(visible=cognito is not None),
                gr.update(visible=False),
                message,
                "",
                [],
                "",
                "",
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
                "",
                "",
            )

        def _stay(
            visitor: Visitor, history: list[dict[str, Any]], box_value: str, note: str
        ) -> tuple[Any, ...]:
            assert visitor.account is not None
            return (
                visitor,
                gr.update(visible=False),
                gr.update(visible=True),
                "",
                describe(visitor.account),
                history,
                box_value,
                note,
            )

        outputs = [
            visitor_state,
            sign_in_panel,
            app_panel,
            sign_in_message,
            whoami,
            chatbot,
            box,
            status,
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
