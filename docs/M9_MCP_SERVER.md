# M9: MCP server 最小实现方案

> 计划文档，2026-10-09。描述要做什么，不表示功能已经存在。

## 目标与范围

把 RetailSupportAgent 已有的订单工具封装成一个 MCP server，供任何 MCP client 调用；写操作的确认和权限校验继续在服务端执行。

**要做的：**

- 一个独立进程的 MCP server，复用现有的工具函数、确认逻辑和数据库层，不另写业务逻辑。
- 读工具和写工具分开暴露，写操作沿用两步确认：先提议，再凭确认令牌执行。
- 一组自动化测试，证明绕过确认、越权访问、重复提交都被服务端挡住。

**明确不做的：**

- 不替换现有的 LangGraph agent 和 FastAPI 接口，MCP 只是多出来的一个入口。
- 不新增业务工具，也不改动 tau2-bench 评测流程。
- 第一版不部署到 AWS，本机跑通并有测试即可。

## 现状盘点

下表是 MCP server 要复用的现有模块，只有最后一行需要替换。

| 模块 | 现有能力 | MCP server 怎么用 |
| --- | --- | --- |
| `domain.py` · `RetailDomain` | 包装 tau2-bench Retail 的 16 个上游工具，其中 7 个是写工具（`WRITE_TOOLS`）；`tool_schemas()` 输出 OpenAI 格式的工具定义 | 直接复用，作为 tool 的执行体 |
| `guardrails.py` · `WriteGuard.review()` | 每次调用按顺序检查：已知工具 → 权限 → 归属 → 审批 → 确认；确认绑定工具名加参数的 SHA-256 指纹 | 每个 tool 执行前都先过它，一行不改 |
| `store.py` · `open_confirmation()` / `confirm()` | 在 PostgreSQL 里记录待确认的参数集；指纹不匹配就不确认 | 复用，确认状态跨进程共享 |
| `store.py` · `execute_once()` | 幂等键 = 会话 + 工具 + 参数 + 确认 id，靠唯一索引保证只执行一次，写入和审计在同一事务 | 复用，MCP 的重试不会重复写入 |
| `auth.py` · `CachedJwks` / `Identity` | 校验 Cognito 的 RS256 JWT，角色来自数据库而不是 token 声明 | 复用为 MCP 的 bearer token 校验 |
| `scope.py` · `AccessScope` | 把读写限制在当前会话的数据切片内，并隐藏敏感字段 | 复用，按身份构造 |
| `agent.py` · `_maybe_consume_confirmation()` | 从用户回复里识别 “yes / confirm” 等词作为确认 | **不能复用**：MCP 下没有用户回复这一轮，只有客户端模型在调工具 |

## 架构设计

新增一个独立进程 `mcp_server.py`，用 MCP Python SDK v2 的 `MCPServer`，走 Streamable HTTP。它和现有 FastAPI 共用同一个数据库和业务模块，但不经过 LangGraph agent：客户端的模型就是新的 agent。

```
                写操作待确认：返回表单，由用户在客户端作答，模型不能代答
        ┌─────────────────────────────────────────────────────────────────┐
        ▼                                                                 │
┌───────────────┐   JWT    ┌──────────────────────┐  已认证   ┌──────────────────┐
│  MCP client   │ ───────▶ │ Streamable HTTP /mcp │ ───────▶ │  Tool handlers   │
│ Claude Code 等│          │   Cognito JWT 校验    │          │ 读工具与写工具分开 │
└───────────────┘          └──────────────────────┘          └──────────────────┘
                                                                     │ 每次调用
                                                                     ▼
┌───────────────┐ apply()  ┌──────────────────────┐ 允许才执行 ┌──────────────────┐
│ RetailDomain  │ ◀─────── │  Store.execute_once  │ ◀──────── │    WriteGuard    │
│tau2 Retail 工具│          │  唯一索引幂等 + 审计   │           │权限·归属·参数指纹确认│
└───────────────┘          └──────────────────────┘           └──────────────────┘
                                      │ 同一事务
                                      ▼
                           ┌──────────────────────┐
                           │      PostgreSQL      │
                           │  确认 · 操作 · 审计表  │
                           └──────────────────────┘
```

- **传输**：Streamable HTTP，端点 `/mcp`。
- **进程**：单进程、有状态，每个会话的 `RetailDomain` 缓存在进程内。
- **会话**：一个 MCP 会话对应一个 `session_id`，格式 `mcp:<subject>`。
- **依赖**：`mcp>=2`，类名 `MCPServer`（v1 的 `FastMCP` 已删除，见 [What's new in v2](https://py.sdk.modelcontextprotocol.io/whats-new/)）。

## Tool 清单与契约

第一版只暴露 7 个工具，覆盖项目声明的三条业务流程（查单、取消、退货）加转人工。工具名和参数沿用 tau2 上游的定义，不另起名字。

| Tool | 参数 | 类型 | 经过的检查 |
| --- | --- | --- | --- |
| `get_user_details` | `user_id` | 读 | 权限 + 数据切片：只能查 token 对应的用户 |
| `get_order_details` | `order_id` | 读 | 权限 + 数据切片，敏感字段脱敏 |
| `get_product_details` | `product_id` | 读 | 目录类，始终允许 |
| `list_all_product_types` | 无 | 读 | 目录类，始终允许 |
| `cancel_pending_order` | `order_id`, `reason` | 写 | 权限 → 归属 → 状态切片 → 参数指纹确认 → 幂等执行 |
| `return_delivered_order_items` | `order_id`, `item_ids`, `payment_method_id` | 写 | 同上 |
| `transfer_to_human_agents` | `summary` | 转人工 | 创建工单，之后本会话不再自动执行任何工具 |

**契约规则：**

- 不注册 `find_user_id_by_email` 和 `find_user_id_by_name_zip`。
- 拒绝时返回 `{"status": <Decision 枚举值>, "reason": "..."}`，不抛异常。
- 写工具的描述里注明“执行前会弹出确认”。
- 换货、改地址、改支付、改商品 4 个写工具放到第二版。

## 写操作确认与权限

写操作用 MCP elicitation 让用户在客户端确认，确认结果写入现有的确认表。

**一次写调用的顺序：**

1. `get_access_token()` 取 subject，用 `Store` 查出 `user_id` 和角色，构造 `SessionState` 和 `AccessScope`。
2. 构造 `ProposedCall`，交给 `WriteGuard.review()`；结果不是 `NEEDS_CONFIRMATION` 就直接返回，不弹确认。
3. `store.open_confirmation()` 记下参数指纹，用 `call.describe()` 生成问题文本，发起 elicitation。
4. 接受 → `store.confirm()`，再跑一次 `review()` 得到 `ALLOW`，调 `store.execute_once()`。拒绝或取消 → 返回 `{"status": "declined"}`。

**代码骨架**（用 SDK v2 的 `Resolve` 写法，新旧协议都能用；标注“待核对”的导入路径文档里没给全）：

```python
from typing import Annotated

from pydantic import BaseModel
from mcp.server import MCPServer
from mcp.server.mcpserver import Elicit, Resolve
from mcp.server.auth.middleware.auth_context import get_access_token
# 待核对：ElicitationResult / AcceptedElicitation 的导入路径


class Confirm(BaseModel):
    ok: bool  # elicitation 只支持扁平的基础类型字段


async def ask_cancel(order_id: str, reason: str) -> Confirm | Elicit[Confirm]:
    session = load_session(get_access_token())  # 身份只来自 token
    call = ProposedCall("cancel_pending_order", {"order_id": order_id, "reason": reason})
    verdict = guard.review(call, session.state)
    if verdict.decision is not Decision.NEEDS_CONFIRMATION:
        return Confirm(ok=False)  # 被拒或已确认：不弹表单
    store.open_confirmation(session.id, call)  # 重复进入时要幂等
    return Elicit(call.describe(), Confirm)  # 问题文本包含全部参数


@mcp.tool()
async def cancel_pending_order(
    order_id: str,
    reason: str,
    confirm: Annotated[ElicitationResult[Confirm], Resolve(ask_cancel)],
) -> dict:
    session = load_session(get_access_token())
    call = ProposedCall("cancel_pending_order", {"order_id": order_id, "reason": reason})
    match confirm:
        case AcceptedElicitation(data=Confirm(ok=True)):
            store.confirm(session.id, call.fingerprint)
            session.state.confirm(call)
        case _:
            pass
    verdict = guard.review(call, session.state)  # 未确认的这里会被挡下
    if not verdict.allowed:
        return {"status": verdict.decision, "reason": verdict.reason}
    outcome = store.execute_once(
        session.id,
        call,
        session.confirmation_id,
        lambda: session.domain.execute(call.name, call.arguments).content,
    )
    return {"status": "executed", "result": outcome.result, "replayed": outcome.replayed}
```

来源：[Elicitation](https://py.sdk.modelcontextprotocol.io/handlers/elicitation/) · [Dependencies](https://py.sdk.modelcontextprotocol.io/handlers/dependencies/) · [Authorization](https://py.sdk.modelcontextprotocol.io/run/authorization/)

## 实现步骤与工作量

合计约 4 个工作日，按顺序做，每一步结束都能单独运行和提交。开一个分支 `feature/mcp-server`，按项目惯例每一步一个小 PR。

1. **依赖与骨架（0.5 天）**：加 `mcp>=2` 依赖，新建 `src/retail_support_agent/mcp_server.py`，只注册 4 个读工具。本地起 Streamable HTTP，用 SDK 的 `Client` 调通。
2. **鉴权（0.5 天）**：写一个 `CognitoTokenVerifier`，内部复用 `auth.py` 的 `CachedJwks` 校验签名和必填字段，返回带 subject 的 `AccessToken`。`AuthSettings.issuer_url` 指向 Cognito 用户池。测试里用现有的 `StaticKeys` 签 token，不连真实 Cognito。
3. **会话装配（0.5 天）**：`load_session()` 把 subject 映射到 `user_id`、角色、`AccessScope` 和 `SessionState`；每个会话的 `RetailDomain` 缓存在进程内（放在 lifespan 对象里）；已确认的指纹从 `store.confirmed_fingerprints()` 恢复。
4. **写工具与确认（1 天）**：`cancel_pending_order`、`return_delivered_order_items` 按上一节的骨架实现；`open_confirmation()` 改成同一指纹重复进入时不新增行。`transfer_to_human_agents` 复用 agent 里现有的转人工处理，抽成共用函数。
5. **测试（1 天）**：见下一节的验收清单。
6. **文档（0.5 天）**：README 加一节 MCP：启动命令、在 Claude Code 里用 `claude mcp add --transport http` 连接、信任边界说明。

**开工前先确认三件事：**

- [ ] 项目的 Python 版本和现有依赖（`pydantic`、`fastapi`、`uvicorn`）与 `mcp` 2.x 没有版本冲突
- [ ] `get_access_token()` 在 `Resolve` 的解析函数里可用；不可用就把权限预检挪到工具体里
- [ ] 要演示的客户端支持 elicitation 表单

## 测试与验收

每一行都是自动化测试，用 2026-07-28 和 `mode="legacy"` 两种协议各跑一遍；鉴权类测试走 HTTP。「产出数据」列是每次运行要记录的数字。

| 场景 | 预期结果 | 产出数据 |
| --- | --- | --- |
| 不带 token、token 过期、签名错误 | HTTP 401，工具不执行 | 3 种情况各自的状态码；工具执行次数 = 0 |
| 读别人的订单 | `denied_slice` | 拒绝次数；新增审计行数 |
| 取消别人的订单 | `denied_ownership` | elicitation 发送次数 = 0 |
| 没有写权限的角色取消订单 | `denied_permission` | elicitation 发送次数 = 0 |
| 用户拒绝或取消确认 | `declined` | 调用前后 `db_hash()` 相同 |
| 用户接受确认 | `executed` | `db_hash()` 改变；`write_executed` 审计 1 条 |
| 确认后改一个参数再调用 | 重新弹确认 | elicitation 发送次数 = 2 |
| 已确认的同一调用并发 8 次 | 只执行 1 次 | executed = 1，replayed = 7 |
| 客户端不支持 elicitation | 写工具失败并说明原因 | 工具执行次数 = 0 |
| 现有 22 个跨客户越权场景经 MCP 重放 | 全部拦下 | blocked / total；与 FastAPI 入口逐场景结果一致的个数 |

每次运行把上表的数字写进一个 JSON 报告（建议 `reports/mcp_acceptance/<日期>.json`），并接入 CI。这些测试都不调模型，不产生费用。
