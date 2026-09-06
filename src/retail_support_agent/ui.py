"""最小 Gradio 演示界面。

M0.3 只做"能把三类业务走通"的最小界面：固定模拟身份、仅绑定 127.0.0.1、
不开公开分享链接。界面完善是 M7 的独立主题。

界面上显式区分三种状态——已回答 / 待确认 / 被拒绝——因为"写操作必须明确确认"
是本项目的核心卖点，不能藏在对话文本里看不出来。
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
    "answered": "已回答",
    "needs_confirmation": "⚠ 待你确认",
    "denied": "✗ 已拒绝",
    "stopped": "■ 会话已停止",
}

INTRO = f"""### RetailSupportAgent · M0.3 最小业务闭环

固定模拟身份：`{DEMO_USER_ID}`（τ²-bench 模拟数据，非真实用户）。
支持订单查询、取消订单、退货申请。

**写操作会先被拦下来要你确认具体参数**——这是设计如此，不是卡住了。
"""


def _new_agent(user_id: str) -> RetailAgent:
    settings = Settings()  # type: ignore[call-arg]
    domain = RetailDomain()
    return RetailAgent(make_chat_fn(settings, ModelConfig()), domain, SessionState(user_id))


def build_demo(user_id: str = DEMO_USER_ID) -> gr.Blocks:
    with gr.Blocks(title="RetailSupportAgent") as demo:
        gr.Markdown(INTRO)
        agent_state = gr.State(None)
        chatbot = gr.Chatbot(type="messages", height=460, label="对话")
        status = gr.Markdown("")
        with gr.Row():
            box = gr.Textbox(placeholder="例如：帮我查一下我的订单", scale=5, show_label=False)
            send = gr.Button("发送", variant="primary", scale=1)
        reset = gr.Button("重开会话")

        def respond(
            message: str, history: list[dict[str, Any]], agent: RetailAgent | None
        ) -> tuple[list[dict[str, Any]], RetailAgent, str, str]:
            if not message.strip():
                return history, agent or _new_agent(user_id), "", ""
            active = agent or _new_agent(user_id)
            result = active.send(message)
            label = STATUS_LABELS.get(result.status, result.status)
            note = f"**状态：{label}**"
            if result.tool_calls:
                note += f" · 已调用工具：{', '.join(result.tool_calls)}"
            if result.stop_reason:
                note += f" · {result.stop_reason}"
            updated = [
                *history,
                {"role": "user", "content": message},
                {"role": "assistant", "content": result.reply},
            ]
            return updated, active, "", note

        def start_over() -> tuple[list[dict[str, Any]], None, str]:
            return [], None, "已重开会话。"

        send.click(respond, [box, chatbot, agent_state], [chatbot, agent_state, box, status])
        box.submit(respond, [box, chatbot, agent_state], [chatbot, agent_state, box, status])
        reset.click(start_over, None, [chatbot, agent_state, status])

    assert isinstance(demo, gr.Blocks)
    return demo


def main() -> None:
    """仅绑定本地地址；不开 share 链接，不提供公开注册。"""
    build_demo().launch(server_name="127.0.0.1", server_port=7860, share=False)


if __name__ == "__main__":
    main()
