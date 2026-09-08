"""Minimal Gradio demo UI.

M0.3 needs only enough interface to run the three flows: fixed simulated identity,
bound to 127.0.0.1, no public share link. A real interface is M7.

The UI shows three states explicitly - answered, awaiting confirmation, denied -
because "writes require explicit confirmation" is this project's central claim and
must not be buried in conversation text.
"""

from __future__ import annotations

from typing import Any

import gradio as gr

from retail_support_agent.agent import RetailAgent
from retail_support_agent.api import DEMO_USER_ID
from retail_support_agent.config import Settings
from retail_support_agent.domain import RetailDomain
from retail_support_agent.guardrails import SessionState
from retail_support_agent.llm import ModelConfig, make_chat_fn

STATUS_LABELS = {
    "answered": "answered",
    "needs_confirmation": "⚠ awaiting your confirmation",
    "denied": "✗ denied",
    "stopped": "■ session stopped",
}

INTRO = f"""### RetailSupportAgent · M0.3 minimal business loop

Fixed simulated identity: `{DEMO_USER_ID}` (τ²-bench simulated data, not a real user).
Supports order lookup, order cancellation and return requests.

Write actions are held for confirmation of their exact arguments. That is by design,
not a hang.
"""


def _new_agent(user_id: str) -> RetailAgent:
    settings = Settings()  # type: ignore[call-arg]
    domain = RetailDomain()
    return RetailAgent(make_chat_fn(settings, ModelConfig()), domain, SessionState(user_id))


def build_demo(user_id: str = DEMO_USER_ID) -> gr.Blocks:
    with gr.Blocks(title="RetailSupportAgent") as demo:
        gr.Markdown(INTRO)
        agent_state = gr.State(None)
        # No `type` argument: gradio 6 removed it because messages is the only format,
        # and passing it raises TypeError. This code already emits {"role", "content"}.
        chatbot = gr.Chatbot(height=460, label="Conversation")
        status = gr.Markdown("")
        with gr.Row():
            box = gr.Textbox(placeholder="e.g. show me my orders", scale=5, show_label=False)
            send = gr.Button("Send", variant="primary", scale=1)
        reset = gr.Button("New session")

        def respond(
            message: str, history: list[dict[str, Any]], agent: RetailAgent | None
        ) -> tuple[list[dict[str, Any]], RetailAgent, str, str]:
            if not message.strip():
                return history, agent or _new_agent(user_id), "", ""
            active = agent or _new_agent(user_id)
            result = active.send(message)
            label = STATUS_LABELS.get(result.status, result.status)
            note = f"**Status: {label}**"
            if result.tool_calls:
                note += f" · tools called: {', '.join(result.tool_calls)}"
            if result.stop_reason:
                note += f" · {result.stop_reason}"
            updated = [
                *history,
                {"role": "user", "content": message},
                {"role": "assistant", "content": result.reply},
            ]
            return updated, active, "", note

        def start_over() -> tuple[list[dict[str, Any]], None, str]:
            return [], None, "Started a new session."

        send.click(respond, [box, chatbot, agent_state], [chatbot, agent_state, box, status])
        box.submit(respond, [box, chatbot, agent_state], [chatbot, agent_state, box, status])
        reset.click(start_over, None, [chatbot, agent_state, status])

    assert isinstance(demo, gr.Blocks)
    return demo


def main() -> None:
    """Binds to localhost only; no share link, no public registration."""
    build_demo().launch(server_name="127.0.0.1", server_port=7860, share=False)


if __name__ == "__main__":
    main()
