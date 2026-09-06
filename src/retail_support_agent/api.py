"""FastAPI 服务：最小业务闭环的 HTTP 入口。

M0.3 阶段使用**固定模拟身份**，只绑定本地地址，不提供注册或公开访问。
真正的认证（Cognito）是 M3 的独立主题，此处不做也不假装做。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from retail_support_agent.agent import RetailAgent
from retail_support_agent.config import Settings
from retail_support_agent.domain import RetailDomain
from retail_support_agent.guardrails import SessionLimits, SessionState
from retail_support_agent.llm import ModelConfig, make_chat_fn

# M0.3 的固定演示身份，来自 τ²-bench 模拟数据，不是真实用户。
# 选它是因为名下同时有 pending 订单（可取消）与 delivered 订单（可退货），
# 三类业务都能在同一身份下演示。
DEMO_USER_ID = "ava_moore_2033"


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    reply: str
    status: str
    tool_calls: list[str] = []
    pending_confirmation: str | None = None
    stop_reason: str | None = None


class SessionRegistry:
    """会话表。同一会话串行处理；M0 不宣称跨进程恢复能力。"""

    def __init__(self, settings: Settings, config: ModelConfig, user_id: str) -> None:
        self._settings = settings
        self._config = config
        self._user_id = user_id
        self._agents: dict[str, RetailAgent] = {}

    def get(self, session_id: str) -> RetailAgent:
        if session_id not in self._agents:
            # 每个会话独立的领域环境，各持一份数据库副本，互不污染
            domain = RetailDomain()
            state = SessionState(user_id=self._user_id, limits=SessionLimits())
            chat_fn = make_chat_fn(self._settings, self._config)
            self._agents[session_id] = RetailAgent(chat_fn, domain, state)
        return self._agents[session_id]

    def reset(self, session_id: str) -> None:
        self._agents.pop(session_id, None)


def create_app(
    settings: Settings | None = None,
    config: ModelConfig | None = None,
    user_id: str = DEMO_USER_ID,
) -> FastAPI:
    resolved_settings = settings or Settings()  # type: ignore[call-arg]
    registry = SessionRegistry(resolved_settings, config or ModelConfig(), user_id)

    app = FastAPI(
        title="RetailSupportAgent",
        version="0.0.0",
        description="零售客服 Agent 最小业务闭环（M0.3，固定模拟身份，仅本地）",
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "milestone": "M0.3", "demo_user_id": user_id}

    @app.post("/chat", response_model=ChatResponse)
    def chat(request: ChatRequest) -> ChatResponse:
        agent = registry.get(request.session_id)
        try:
            result = agent.send(request.message)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"模型或工具调用失败：{exc}") from exc
        return ChatResponse(
            reply=result.reply,
            status=result.status,
            tool_calls=result.tool_calls,
            pending_confirmation=result.pending_confirmation,
            stop_reason=result.stop_reason,
        )

    @app.post("/session/{session_id}/reset")
    def reset(session_id: str) -> dict[str, str]:
        registry.reset(session_id)
        return {"status": "reset", "session_id": session_id}

    return app
