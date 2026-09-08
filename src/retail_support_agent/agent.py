"""LangGraph 单 Agent：最小业务闭环。

流程与 DEVELOPMENT_PLAN.md M0.3 一致：
    查询订单 → 必要补问 → 判断政策 → 明确确认 → 校验 → 执行 → 解释实际结果

关键设计：**确认与归属校验不由模型决定**。模型只能"提议"工具调用，
是否放行由 `guardrails.WriteGuard` 判定；写操作必须先拿到针对该组具体参数的确认。
模型即使自称"已确认"也过不了这一关。

回答必须基于工具真实返回的状态——退货申请成功不等于钱已到账，
这一点写进系统提示词，并由评测的 DB 分量兜底。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from retail_support_agent.domain import HANDOFF_TOOLS, WRITE_TOOLS, RetailDomain, ToolResult
from retail_support_agent.guardrails import (
    Decision,
    ProposedCall,
    SessionState,
    WriteGuard,
)
from retail_support_agent.store import Store

# 注意：这份提示词只用于**应用演示**。
# 标准 τ²-bench 评测走上游自己的身份识别协议与 Agent 提示词，两者分别记录，
# 不能把这里的固定身份用于改变标准评分条件（AGENTS.md 与 DEVELOPMENT_PLAN.md 的要求）。
SYSTEM_PROMPT_TEMPLATE = """你是零售客服 Agent。

**身份已由应用层在登录环节完成验证，当前用户 ID 就是 {user_id}。**
因此不要再向用户索要邮箱或姓名邮编做身份验证，也不要调用 find_user_id_* 来"确认身份"；
需要用户资料时直接用 {user_id} 调用 get_user_details。

除身份识别环节外，必须遵守下面这份领域政策（原文，不得自行放宽）：

{policy}

额外的硬性要求：

1. 只能处理当前用户 {user_id} 名下的订单。涉及其他用户的请求一律拒绝并说明原因。
2. 信息不足时先向用户补问，不要猜测订单号、商品或数量。
3. 确认分两段，两段都必须走完：
   先按上述政策向用户解释操作细节并取得口头同意（这是政策要求，不能跳过）；
   用户同意后，直接发起参数完整的工具调用。系统会再拦一次，把完整参数
   展示给用户做最终确认，通过后才真正执行。
   你不能代替用户确认，也不要声称"已为你确认"或"已完成"。
4. 回答必须与工具返回的真实状态一致。退货或换货**申请成功不等于款项已经退回**，
   要如实说明当前所处的状态。
5. 无法处理时，说明原因或转人工，不要编造已完成。
"""


class AgentState(TypedDict, total=False):
    """LangGraph 状态。messages 用 add_messages 累积。"""

    messages: Annotated[list[dict[str, Any]], add_messages]
    status: str
    stop_reason: str


@dataclass
class TurnResult:
    """一轮对话的结果，供 API 与界面使用。"""

    reply: str
    status: Literal["answered", "needs_confirmation", "denied", "stopped"]
    tool_calls: list[str] = field(default_factory=list)
    pending_confirmation: str | None = None
    stop_reason: str | None = None


ChatFn = Callable[[list[dict[str, Any]], list[dict[str, Any]]], Any]
"""(messages, tools) -> 形如 OpenAI ChatCompletion 的响应。便于用假模型做测试。"""


def _tool_calls_of(message: Any) -> list[Any]:
    return list(getattr(message, "tool_calls", None) or [])


def _parse_arguments(raw: str | None) -> dict[str, Any] | None:
    """模型给的参数可能不是合法 JSON——这属于业务失败，要如实回传。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class RetailAgent:
    """一个会话一个实例：独立的领域环境、独立的状态。"""

    def __init__(
        self,
        chat_fn: ChatFn,
        domain: RetailDomain,
        session: SessionState,
        store: Store | None = None,
        session_id: str | None = None,
    ) -> None:
        self._chat = chat_fn
        self._domain = domain
        self._session = session
        # store 为 None 时全部退化成无操作：数据库是能力，不是运行的前提。
        # 没配库也要能起服务、能跑评测——评测回路里没有 PostgreSQL。
        self._store = store
        self._session_id = session_id
        self._confirmation_id: int | None = None
        if self._store is not None and self._session_id is not None:
            self._store.ensure_session(self._session_id, session.user_id)
        self._guard = WriteGuard(domain.tool_names(), domain.order_owner)
        # 本轮已真正执行的工具。**必须按轮累计，不能只记最后一步**：
        # 一轮通常是 think(要调工具) -> act(执行) -> think(给出答复)，
        # 最后那步没有工具调用，若在那里现造一个空列表，就把前面执行过的工具
        # 全丢了——读类查询于是永远报「未调用任何工具」，而它明明查了数据库。
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

    # ---- LangGraph 装配 -------------------------------------------------

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
        # 只有真正执行了工具才回到模型解释结果；待确认/被停止必须当场结束本轮，
        # 否则模型会被再调一次，把"等你确认"这个状态覆盖掉。
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

    # ---- 节点 -----------------------------------------------------------

    def _node_think(self, state: AgentState) -> AgentState:
        """调模型。先检查停止条件——超限时不再发请求，避免无谓计费。"""
        stop = self._session.stop_reason()
        if stop is not None:
            self._last = TurnResult(
                reply=f"本次会话已停止：{stop}。请重新发起或转人工处理。",
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
        """逐个裁决并执行模型提议的工具调用。"""
        pending_calls = _tool_calls_of_last_assistant(self._messages)

        for call in pending_calls:
            arguments = _parse_arguments(call["function"].get("arguments"))
            if arguments is None:
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": "参数不是合法的 JSON 对象，未执行。请修正后重试。",
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
                        "content": "未执行：该写操作需要用户针对具体参数明确确认。",
                    }
                )
                self._last = TurnResult(
                    reply=verdict.reason,
                    status="needs_confirmation",
                    tool_calls=list(self._turn_tools),
                    pending_confirmation=proposed.describe(),
                )
                return {"status": "needs_confirmation"}

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
                self._last = TurnResult(
                    reply="已转交人工客服处理。",
                    status="answered",
                    tool_calls=list(self._turn_tools),
                )
                return {"status": "answered"}

        return {"status": "tool_executed"}

    # ---- 对外接口 -------------------------------------------------------

    def send(self, user_message: str) -> TurnResult:
        """处理一条用户消息。同一会话必须串行调用。"""
        self._maybe_consume_confirmation(user_message)
        self._messages.append({"role": "user", "content": user_message})
        self._journal("user", user_message, [])
        self._last = None
        self._turn_tools = []
        self._graph.invoke({"messages": []})
        result = self._last or TurnResult(
            reply="（模型未给出回答）", status="answered", tool_calls=list(self._turn_tools)
        )
        self._journal("assistant", result.reply, result.tool_calls)
        return result

    # ---- 落库（store 为 None 时全是无操作） -------------------------------

    def _journal(self, role: str, content: str, tool_calls: list[str]) -> None:
        """把一轮消息写进 messages 表。

        序号由库里的最大值 +1 得来，`(session_id, seq)` 唯一——同一轮重发不会
        插出第二条。落库失败不该让一次已经成功的对话失败，所以只在这里吞掉
        重复，其他异常照常抛出（静默吞掉写库失败等于假装记了审计）。
        """
        if self._store is None or self._session_id is None:
            return
        seq = self._store.next_seq(self._session_id)
        self._store.append_message(self._session_id, seq, role, content, tool_calls)

    def _record_confirmation_request(self, proposed: ProposedCall) -> None:
        if self._store is None or self._session_id is None:
            return
        self._store.open_confirmation(self._session_id, proposed)

    def _execute(self, proposed: ProposedCall) -> ToolResult:
        """执行一次工具调用；写操作走幂等路径。

        读操作不记 operations：它没有副作用，重复执行也不会造成第二次改动，
        为它写一行操作记录只会让审计表被读噪声淹没。
        """
        if self._store is None or self._session_id is None or proposed.name not in WRITE_TOOLS:
            return self._domain.execute(proposed.name, proposed.arguments)

        outcome = self._store.execute_once(
            self._session_id,
            proposed,
            self._confirmation_id,
            lambda: self._domain.execute(proposed.name, proposed.arguments).content,
        )
        # 重放时不再碰领域环境：那正是幂等要挡住的第二次改动。
        return ToolResult(name=proposed.name, content=outcome.result, ok=True)

    def _maybe_consume_confirmation(self, user_message: str) -> None:
        """把用户的肯定答复登记为对**当前挂起那组参数**的确认。"""
        pending = self._session.pending
        if pending is None:
            return
        if _is_affirmative(user_message):
            self._session.confirm(pending)
            if self._store is not None and self._session_id is not None:
                # 指纹对不上时 confirm 返回 None——用户同意的是当时列给他看的
                # 那组参数，模型换了参数就得重新问。
                self._confirmation_id = self._store.confirm(self._session_id, pending.fingerprint)
        else:
            self._session.pending = None


AFFIRMATIVE = ("确认", "确定", "同意", "可以", "是的", "好的", "yes", "y", "ok", "确认执行")


def _is_affirmative(text: str) -> bool:
    """判定是否为明确肯定。含否定词时一律不算确认。"""
    lowered = text.strip().lower()
    if any(word in lowered for word in ("不", "别", "取消确认", "no", "cancel")):
        return False
    return any(word in lowered for word in AFFIRMATIVE)


def _assistant_message(message: Any, calls: list[Any]) -> dict[str, Any]:
    """把模型响应转成可回传的消息体，保留 tool_calls 与 reasoning_content。

    `reasoning_content` 必须回传：DeepSeek 文档明确规定，**请求携带 `tools` 参数时**
    「历史轮次的 reasoning_content 均应回传给 API，并会被拼接进上下文」。
    我们每一轮都带 tools，所以丢掉它等于让模型每轮都忘记自己刚才的推理——
    不报错，只是变笨。未带 tools 的请求则相反：回传也会被忽略，所以只在有它时附上。
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
