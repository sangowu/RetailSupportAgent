"""What a confirmation form says a write will do, in words a person can check (M11).

`ProposedCall.describe()` lists the arguments, which is exact but opaque:
`new_item_ids: ["7493556126"]` does not tell anyone what they are agreeing to. The agent
explains an exchange in chat before it asks; over MCP the client's model may or may not,
and the form is the only text the server controls. So the MCP server puts a summary above
the argument list for the writes whose arguments are identifiers.

The summary explains; it does not decide. It is built only after the guard has said the
only thing missing is the user's answer, it is not stored, and the fingerprint is still
the arguments'. A value that cannot be read is said to be unreadable rather than refused:
refusing is the guard's job, and upstream's when the call runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from retail_support_agent.guardrails import ProposedCall

# One read through the session's domain: the parsed answer, or None if the tool failed.
Reader = Callable[[str, dict[str, Any]], dict[str, Any] | None]

ITEM_CHANGES = frozenset({"exchange_delivered_order_items", "modify_pending_order_items"})


def summarize(call: ProposedCall, read: Reader) -> str | None:
    """The summary for this write, or None for a write that needs none."""
    if call.name in ITEM_CHANGES:
        return _item_change(call, read)
    if call.name == "modify_pending_order_address":
        return _address_change(call, read)
    if call.name == "modify_pending_order_payment":
        return _payment_change(call, read)
    return None


def reader(execute: Callable[[str, dict[str, Any]], Any]) -> Reader:
    """A `Reader` over a domain's `execute`, which answers with a `ToolResult`."""

    def read(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        result = execute(name, arguments)
        if not result.ok:
            return None
        try:
            parsed = json.loads(result.content)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    return read


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def _options_changed(old: dict[str, Any], new: dict[str, Any]) -> str:
    changed = [
        f"{key} {old.get(key)} → {value}" for key, value in new.items() if old.get(key) != value
    ]
    return ", ".join(changed) if changed else "same options"


def _item_change(call: ProposedCall, read: Reader) -> str:
    args = call.arguments
    order_id = str(args.get("order_id"))
    old_ids = [str(item) for item in args.get("item_ids") or []]
    new_ids = [str(item) for item in args.get("new_item_ids") or []]
    payment = str(args.get("payment_method_id"))
    verb = "Exchange items" if call.name == "exchange_delivered_order_items" else "Change items"
    lines = [f"{verb} in order {order_id}:"]

    order = read("get_order_details", {"order_id": order_id})
    in_order = {str(item["item_id"]): item for item in (order or {}).get("items", [])}
    if len(old_ids) != len(new_ids):
        lines.append(
            f"- {len(old_ids)} item(s) to replace but {len(new_ids)} new item(s): these "
            "must pair up one to one."
        )

    difference = 0.0
    complete = len(old_ids) == len(new_ids)
    for old_id, new_id in zip(old_ids, new_ids, strict=False):
        old = in_order.get(old_id)
        new = read("get_item_details", {"item_id": new_id})
        if old is None:
            lines.append(f"- item {old_id}: not found in this order.")
            complete = False
            continue
        if new is None:
            lines.append(f"- {old['name']} (item {old_id}) → item {new_id}: not found.")
            complete = False
            continue
        old_price, new_price = float(old["price"]), float(new["price"])
        difference += new_price - old_price
        options = _options_changed(old.get("options") or {}, new.get("options") or {})
        lines.append(f"- {old['name']}: {options}; {_money(old_price)} → {_money(new_price)}.")

    if not complete:
        lines.append("Price difference: cannot be worked out from these items.")
    elif difference > 0:
        lines.append(f"Price difference: {_money(difference)} charged to {payment}.")
    elif difference < 0:
        lines.append(f"Price difference: {_money(-difference)} refunded to {payment}.")
    else:
        lines.append("Price difference: none.")
    lines.append("Prices are as of now; the amount is worked out again when it runs.")
    return "\n".join(lines)


def _address(address: dict[str, Any]) -> str:
    parts = [
        address.get("address1"),
        address.get("address2"),
        address.get("city"),
        f"{address.get('state', '')} {address.get('zip', '')}".strip(),
        address.get("country"),
    ]
    return ", ".join(str(part) for part in parts if part)


def _address_change(call: ProposedCall, read: Reader) -> str:
    order_id = str(call.arguments.get("order_id"))
    order = read("get_order_details", {"order_id": order_id})
    current = _address(order["address"]) if order and order.get("address") else "unknown"
    return "\n".join(
        [
            f"Ship order {order_id} to: {_address(call.arguments)}.",
            f"Currently: {current}.",
        ]
    )


def _payment_change(call: ProposedCall, read: Reader) -> str:
    order_id = str(call.arguments.get("order_id"))
    payment = str(call.arguments.get("payment_method_id"))
    order = read("get_order_details", {"order_id": order_id})
    if order is None:
        return f"Pay for order {order_id} with {payment}. The order could not be read."
    history = order.get("payment_history") or []
    current = ", ".join(str(entry.get("payment_method_id")) for entry in history) or "unknown"
    total = sum(float(item.get("price", 0)) for item in order.get("items", []))
    return "\n".join(
        [
            f"Pay for order {order_id} ({_money(total)}) with {payment}.",
            f"Currently paid with: {current}.",
        ]
    )
