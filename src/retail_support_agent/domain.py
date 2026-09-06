"""τ²-bench Retail 领域适配层。

只做"接线"，不重写上游的工具、政策或数据：工具实现与政策文本全部来自
按固定 commit 安装的 `tau2` 包，业务数据来自 `third_party/` 下经 SHA256 校验的检出。

每个会话持有独立的 Environment 实例（各自一份 DB 副本），避免会话之间互相污染——
这是 DEVELOPMENT_PLAN.md M0.2 明确要求的隔离条件。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from retail_support_agent.tau2_pin import REPO_ROOT

DEFAULT_DATA_DIR = REPO_ROOT / "third_party" / "tau2-bench" / "data"

# 会改变数据库状态的工具。确认与归属校验只针对这些工具，读操作不拦。
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "cancel_pending_order",
        "return_delivered_order_items",
        "exchange_delivered_order_items",
        "modify_pending_order_items",
        "modify_pending_order_address",
        "modify_pending_order_payment",
        "modify_user_address",
    }
)

# 需要人工介入的终止型工具：调用后不应再继续自动执行。
HANDOFF_TOOLS: frozenset[str] = frozenset({"transfer_to_human_agents"})


class DomainError(RuntimeError):
    """领域环境无法加载或工具调用失败。"""


def ensure_data_dir(data_dir: Path | None = None) -> Path:
    """设置 TAU2_DATA_DIR，让上游包读到我们校验过的那份数据。

    必须在 import tau2 之前调用——上游在模块导入时就解析了数据目录。
    """
    resolved = data_dir or Path(os.getenv("TAU2_DATA_DIR") or DEFAULT_DATA_DIR)
    if not resolved.is_dir():
        raise DomainError(
            f"τ²-bench 数据目录不存在：{resolved}\n先运行 uv run python scripts/fetch_tau2.py"
        )
    os.environ["TAU2_DATA_DIR"] = str(resolved)
    return resolved


@dataclass
class ToolResult:
    """一次工具调用的结果。失败也要如实返回给模型，不能悄悄吞掉。"""

    name: str
    ok: bool
    content: str

    def as_tool_message(self, tool_call_id: str) -> dict[str, str]:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": self.content}


def _serialize(raw: Any) -> str:
    """把上游工具的返回值转成模型能读的文本。

    上游工具返回三种东西：已经是字符串的、pydantic 模型、以及普通容器。
    pydantic 模型必须走 model_dump_json——用 json.dumps(default=str) 会把整个对象
    压成一个字符串字面量，后续再解析就拿不到字段了（这个坑在真实数据上才暴露）。
    """
    if isinstance(raw, str):
        return raw
    dump = getattr(raw, "model_dump_json", None)
    if callable(dump):
        return str(dump())
    return json.dumps(raw, ensure_ascii=False, default=str)


class RetailDomain:
    """一个会话一个实例，持有独立的 Environment 与数据库副本。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        ensure_data_dir(data_dir)
        # 延迟导入：上游包在导入时读取 TAU2_DATA_DIR，必须先设好环境变量
        from tau2.domains.retail.environment import get_environment

        self._env = get_environment()

    @property
    def policy(self) -> str:
        """上游原始政策文本。不改写、不摘要——摘要会悄悄改变评测条件。"""
        policy: str = self._env.get_policy()
        return policy

    def tool_schemas(self) -> list[dict[str, Any]]:
        """上游工具的 OpenAI 格式定义，直接透传。"""
        return [tool.openai_schema for tool in self._env.get_tools()]

    def tool_names(self) -> set[str]:
        return {tool.name for tool in self._env.get_tools()}

    def db_hash(self) -> str:
        """数据库状态指纹，用于确认写操作是否真的改变了状态。"""
        return str(self._env.get_db_hash())

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """执行一次工具调用。上游抛错时把错误原文回传给模型，让它据实解释。"""
        try:
            raw = self._env.use_tool(name, **arguments)
        except Exception as exc:
            return ToolResult(name=name, ok=False, content=f"工具执行失败：{exc}")
        return ToolResult(name=name, ok=True, content=_serialize(raw))

    def order_owner(self, order_id: str) -> str | None:
        """查订单归属的 user_id；订单不存在或结构异常时返回 None（按拒绝处理）。"""
        result = self.execute("get_order_details", {"order_id": order_id})
        if not result.ok:
            return None
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        owner = payload.get("user_id")
        return str(owner) if owner is not None else None
