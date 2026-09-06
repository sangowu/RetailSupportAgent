"""跑通三类业务的演示流程，并把完整对话与数据库状态变化存档。

**会发起真实模型调用并产生费用。**

M0.3 的验收标准是"三类演示流程可复现"。本脚本把这件事变成一条命令：
每个流程记录逐轮回复、状态、实际执行的工具，以及**操作前后的数据库指纹**——
指纹没变就说明写操作没真正落地，光看模型嘴上说"已取消"不算数。

用法：
    uv run python scripts/demo_flows.py
    uv run python scripts/demo_flows.py --flow cancel
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_support_agent.agent import RetailAgent
from retail_support_agent.api import DEMO_USER_ID
from retail_support_agent.config import Settings
from retail_support_agent.domain import RetailDomain
from retail_support_agent.guardrails import SessionState
from retail_support_agent.llm import ModelConfig, make_chat_fn
from retail_support_agent.tau2_pin import REPO_ROOT

OUTPUT_ROOT = REPO_ROOT / "evaluation" / "demo_runs"

# 该演示身份名下的订单（来自 τ²-bench 模拟数据）：
#   #W4135875 pending    -> 可取消
#   #W4817420 delivered  -> 可退货
FLOWS: dict[str, list[str]] = {
    "query": [
        "你好，我想看看我名下都有哪些订单。",
        "订单 #W4135875 现在是什么状态？",
    ],
    # 写操作是两段式确认：政策要求 Agent 先解释并取得同意，
    # 系统再拿完整参数要一次最终确认。所以写流程比读流程多一轮。
    #
    # 退货流程多给一轮，是被实测逼出来的：选定模型 deepseek-v4-pro 在写操作上
    # **偏保守**——参数越多越倾向于再确认一次而不是动手。退货要带 5 个商品 ID，
    # 于是它在第三轮又问了一遍"请回复是"，写操作始终没发出，DB 纹丝不动。
    # 同样的三轮在取消流程上是够的（只带一个订单号）。
    # 这不是护栏的问题：护栏正确地扣住了写操作，是模型自己没有再次发起调用。
    "cancel": [
        "我想取消订单 #W4135875，我不想要了。",
        "对，就按这个原因取消。",
        "确认执行。",
    ],
    "return": [
        "订单 #W4817420 我收到了但不合适，想申请全部退货，退回原支付方式。",
        "对，全部退，退回原支付方式。",
        "确认执行。",
        "是，确认无误，请提交。",
    ],
    # 越权尝试：该订单不属于当前身份，必须被拒绝
    "ownership_denied": [
        "帮我取消订单 #W2378156。",
        "确认取消。",
    ],
}


@dataclass
class TurnRecord:
    user: str
    reply: str
    status: str
    tool_calls: list[str] = field(default_factory=list)
    pending_confirmation: str | None = None


@dataclass
class FlowRecord:
    flow: str
    db_hash_before: str
    db_hash_after: str
    turns: list[TurnRecord] = field(default_factory=list)

    @property
    def db_changed(self) -> bool:
        return self.db_hash_before != self.db_hash_after


def run_flow(name: str, messages: list[str], settings: Settings, config: ModelConfig) -> FlowRecord:
    """每个流程用全新的领域实例，避免流程之间互相影响。"""
    domain = RetailDomain()
    agent = RetailAgent(make_chat_fn(settings, config), domain, SessionState(user_id=DEMO_USER_ID))
    record = FlowRecord(flow=name, db_hash_before=domain.db_hash(), db_hash_after="")

    print(f"\n=== 流程 {name} ===", flush=True)
    for message in messages:
        print(f"  用户 > {message}")
        result = agent.send(message)
        print(f"  Agent> [{result.status}] {result.reply.strip()[:220]}")
        if result.tool_calls:
            print(f"         已执行工具：{', '.join(result.tool_calls)}")
        record.turns.append(
            TurnRecord(
                user=message,
                reply=result.reply,
                status=result.status,
                tool_calls=result.tool_calls,
                pending_confirmation=result.pending_confirmation,
            )
        )

    record.db_hash_after = domain.db_hash()
    print(f"  数据库状态：{'已改变' if record.db_changed else '未改变'}")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="M0.3 三类业务演示（会产生真实费用）")
    parser.add_argument("--flow", choices=sorted(FLOWS), default=None)
    parser.add_argument("--model", default=None, help="覆盖默认的参照模型")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        print(
            f"配置读取失败（检查 .env 中的 DASHSCOPE_API_KEY）：{type(exc).__name__}",
            file=sys.stderr,
        )
        return 2

    config = ModelConfig(model_id=args.model) if args.model else ModelConfig()
    selected = {args.flow: FLOWS[args.flow]} if args.flow else FLOWS

    records = [run_flow(name, msgs, settings, config) for name, msgs in selected.items()]

    run_dir = OUTPUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "run_at": datetime.now(UTC).isoformat(),
        "demo_user_id": DEMO_USER_ID,
        "model": config.model_id,
        "thinking": config.thinking.as_extra_body(),
        "note": "模拟业务数据，非真实系统。数据库指纹用于验证写操作是否真的落地。",
        "flows": [asdict(record) | {"db_changed": record.db_changed} for record in records],
    }
    (run_dir / "flows.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n存档：{run_dir / 'flows.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
