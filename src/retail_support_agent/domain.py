"""Adapter for the τ²-bench Retail domain.

Wiring only. Tool implementations and the policy text come from the `tau2` package
installed at a pinned commit; the business data comes from the SHA256-verified
checkout under `third_party/`. Nothing here is rewritten or summarized.

Each session holds its own Environment instance, and therefore its own database
copy, so sessions cannot affect each other. DEVELOPMENT_PLAN.md M0.2 requires this
isolation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from retail_support_agent.tau2_pin import REPO_ROOT

DEFAULT_DATA_DIR = REPO_ROOT / "third_party" / "tau2-bench" / "data"

# Tools that change database state. Confirmation and ownership checks apply to these
# only; reads pass through.
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

# Terminal tools that hand off to a human. Nothing should run automatically after one.
HANDOFF_TOOLS: frozenset[str] = frozenset({"transfer_to_human_agents"})


class DomainError(RuntimeError):
    """The domain environment could not be loaded, or a tool call failed."""


def ensure_data_dir(data_dir: Path | None = None) -> Path:
    """Point TAU2_DATA_DIR at the verified data checkout.

    Must run before `import tau2`: upstream resolves the data directory at module
    import time.
    """
    resolved = data_dir or Path(os.getenv("TAU2_DATA_DIR") or DEFAULT_DATA_DIR)
    if not resolved.is_dir():
        raise DomainError(
            f"τ²-bench data directory not found: {resolved}\n"
            "Run uv run python scripts/fetch_tau2.py first."
        )
    os.environ["TAU2_DATA_DIR"] = str(resolved)
    return resolved


@dataclass
class ToolResult:
    """Result of one tool call. Failures are returned to the model as they are."""

    name: str
    ok: bool
    content: str

    def as_tool_message(self, tool_call_id: str) -> dict[str, str]:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": self.content}


def _serialize(raw: Any) -> str:
    """Turn an upstream tool return value into text the model can read.

    Upstream returns strings, pydantic models, or plain containers. Pydantic models
    must go through model_dump_json: json.dumps(default=str) collapses the object
    into a single string literal, and the fields are then unrecoverable. This only
    shows up on real data.
    """
    if isinstance(raw, str):
        return raw
    dump = getattr(raw, "model_dump_json", None)
    if callable(dump):
        return str(dump())
    return json.dumps(raw, ensure_ascii=False, default=str)


class RetailDomain:
    """One instance per session, holding its own Environment and database copy."""

    def __init__(self, data_dir: Path | None = None) -> None:
        ensure_data_dir(data_dir)
        # Imported late: upstream reads TAU2_DATA_DIR at import time.
        from tau2.domains.retail.environment import get_environment

        self._env = get_environment()

    @property
    def policy(self) -> str:
        """The upstream policy text, unmodified. Summarizing it changes the test."""
        policy: str = self._env.get_policy()
        return policy

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Upstream tool definitions in OpenAI format, passed through."""
        return [tool.openai_schema for tool in self._env.get_tools()]

    def tool_names(self) -> set[str]:
        return {tool.name for tool in self._env.get_tools()}

    def db_hash(self) -> str:
        """Database state fingerprint, used to check whether a write changed anything."""
        return str(self._env.get_db_hash())

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Run one tool call. Upstream errors are passed back to the model verbatim."""
        try:
            raw = self._env.use_tool(name, **arguments)
        except Exception as exc:
            return ToolResult(name=name, ok=False, content=f"Tool call failed: {exc}")
        return ToolResult(name=name, ok=True, content=_serialize(raw))

    def order_owner(self, order_id: str) -> str | None:
        """The user_id owning an order.

        Returns None when the order does not exist or the payload is malformed;
        callers treat None as a denial.
        """
        return self._order_field(order_id, "user_id")

    def order_status(self, order_id: str) -> str | None:
        """The order's lifecycle stage: pending, processed, delivered or cancelled.

        The dataset has no slice column, so this field is the slice: "pending orders
        only" is derived from the record rather than from a marker stored beside it.
        Returns None on a missing or malformed order, which an enforcing scope treats
        as a denial.
        """
        return self._order_field(order_id, "status")

    def _order_field(self, order_id: str, field_name: str) -> str | None:
        """One string field of an order, or None if it cannot be read.

        A failed parse returns None rather than raising: the callers are access checks,
        and an unreadable order must deny rather than crash the turn.
        """
        result = self.execute("get_order_details", {"order_id": order_id})
        if not result.ok:
            return None
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get(field_name)
        return str(value) if value is not None else None
