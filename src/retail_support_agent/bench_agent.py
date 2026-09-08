"""把本项目的护栏接进 τ²-bench，让自研实现成为**被测对象**。

## 为什么需要这个模块

M0 的 0.8509 衡量的不是本项目的实现：评测跑的是上游自带的 `llm_agent`
（`build_run_config()` 里 `agent="llm_agent"`），而写操作两段确认、确认绑定
具体参数、订单归属校验这些本项目真正的主张，全在 `guardrails.py` 里，
**只被 demo_flows 和单元测试用过**。上游那个 agent 里连 `confirm` 这个词都没有。

本模块注册一个 `rsa_guarded`，让护栏第一次面对 114 道真实任务。

## 继承而不是重写

系统提示词、消息组织、LLM 调用一律沿用上游 `LLMAgent`，只覆写
`generate_next_message` 这一处。这样与 `baseline-v2` 的对比才是「有没有护栏」，
而不是「两套不同的实现」——后者什么也说明不了。

## 与 M0.3 应用路径的一处关键差异

应用路径用固定模拟身份（`SessionState.user_id` 是给定的）。基准里没有这个前提：
Agent 必须按上游政策**先完成身份识别**才知道在为谁服务。所以这里的身份是
**从对话中建立**的：看 `find_user_id_by_*` 的返回值。身份未建立前一切写操作拒绝。

同理，订单归属**只用对话里已经查到的信息**判断，不额外调用工具去查——
额外调用会在成绩、成本和 action metrics 之外产生不被记录的副作用，
让这一轮的数字不再可解释。查不到归属就要求先查，这本来也是政策要求的。
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ConfigDict

from retail_support_agent.domain import WRITE_TOOLS
from retail_support_agent.guardrails import (
    Decision,
    GuardVerdict,
    ProposedCall,
    SessionLimits,
    SessionState,
)

AGENT_NAME = "rsa_guarded"

# 这两个工具的返回值就是用户 id（上游 retail 政策规定的身份识别入口）。
IDENTITY_TOOLS = frozenset({"find_user_id_by_email", "find_user_id_by_name_zip"})

# 从这个工具的返回里能读到订单归属。
ORDER_LOOKUP_TOOL = "get_order_details"

NO_IDENTITY_REASON = (
    "The customer has not been authenticated yet. Refusing to run a write action. "
    "Call find_user_id_by_email or find_user_id_by_name_zip first."
)
UNKNOWN_OWNER_REASON = (
    "Order {order_id} has not been looked up, so its owner is unknown. "
    "Refusing to run a write action. Call get_order_details on it first."
)


def _clean(raw: Any) -> str:
    """上游工具返回值可能是 pydantic 模型，也可能已经是字符串。"""
    if isinstance(raw, str):
        return raw
    dump = getattr(raw, "model_dump_json", None)
    if callable(dump):
        return str(dump())
    return json.dumps(raw, ensure_ascii=False, default=str)


class GuardState:
    """一题一份的护栏状态。

    **不能用模块级全局**：批量运行是并发的，全局变量会让不同题互相污染，
    而污染的表现是"某几题莫名其妙通过了确认"——一个不会报错的错误。
    """

    def __init__(self) -> None:
        self.identity: str | None = None
        self.order_owner: dict[str, str] = {}
        # ToolMessage 只带 id 不带工具名，所以要自己记住 id -> 名字，
        # 否则工具结果回来时不知道它是身份查询还是订单查询。
        self.call_names: dict[str, str] = {}
        self.confirmed: set[str] = set()
        self.pending: ProposedCall | None = None
        # 留痕：这一题里护栏一共拦下了什么。跑完可据此把退步的题分类。
        self.interventions: list[dict[str, str]] = []

    def as_session(self) -> SessionState:
        """借用应用侧的 SessionState 来复用 WriteGuard 的裁决逻辑。

        身份未建立时不该走到这里——调用方要先拦掉，因为 SessionState.user_id
        是必填的 str，塞一个占位值会让归属校验拿它去比对，判出误导性的结论。
        """
        assert self.identity is not None
        state = SessionState(user_id=self.identity, limits=SessionLimits())
        state.confirmed_fingerprints = set(self.confirmed)
        return state


def observe_tool_result(guard: GuardState, name: str, content: str) -> None:
    """从工具结果里更新身份与订单归属。

    解析失败一律当作"没学到东西"，绝不猜：猜错的后果是放行一次本该拦下的写操作。
    """
    if name in IDENTITY_TOOLS:
        candidate = content.strip().strip('"')
        # 用户 id 形如 ava_moore_2033；上游查不到时返回的是一段错误说明
        if candidate and " " not in candidate and "\n" not in candidate:
            guard.identity = candidate
        return

    if name == ORDER_LOOKUP_TOOL:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        order_id = payload.get("order_id")
        user_id = payload.get("user_id")
        if isinstance(order_id, str) and isinstance(user_id, str):
            guard.order_owner[order_id] = user_id


def review_call(guard: GuardState, known_tools: set[str], call: ProposedCall) -> GuardVerdict:
    """对一次提议的工具调用做裁决。

    读操作直接放行；写操作依次过三关：身份已建立 -> 归属已知且相符 -> 已确认。

    归属的**唯一来源是对话里已经查到的信息**（`guard.order_owner`）。
    这一点必须显式接进 `WriteGuard`：它本来会自己调 `owner_lookup` 去查，
    而在基准里我们不额外调工具（理由见模块开头），所以把已学到的映射喂给它。
    早先给了个恒返回 None 的 lookup，结果每一次写操作都被判成"订单不存在"——
    看起来很安全，实际上把所有写操作都拦死了，且原因完全不可解释。
    """
    from retail_support_agent.guardrails import WriteGuard

    guard_impl = WriteGuard(known_tools, guard.order_owner.get)

    if call.name not in WRITE_TOOLS:
        read_verdict: GuardVerdict = guard_impl.review(call, _read_only_session(guard))
        return read_verdict

    if guard.identity is None:
        return GuardVerdict(Decision.DENIED_OWNERSHIP, NO_IDENTITY_REASON, call)

    order_id = call.arguments.get("order_id")
    if isinstance(order_id, str) and order_id not in guard.order_owner:
        return GuardVerdict(
            Decision.DENIED_OWNERSHIP, UNKNOWN_OWNER_REASON.format(order_id=order_id), call
        )

    verdict: GuardVerdict = guard_impl.review(call, guard.as_session())
    return verdict


def _read_only_session(guard: GuardState) -> SessionState:
    """读操作的裁决用不到身份，但 WriteGuard 的签名要一个 SessionState。"""
    return SessionState(user_id=guard.identity or "", limits=SessionLimits())


# 明确的拒绝措辞：出现在答复任何位置都算否定。
# 裸词 "don't" / "do not" 曾经在整句范围内一票否决，代价是任务 80：用户说
# "my Spanish is decent but I don't think that's Spanish! ... YES — go ahead!"
# ——一句谈论语言的话否掉了同一条消息里毫不含糊的同意。所以否定必须**指向动作**
# 才算数：否定词后面若干字内要跟着一个动作动词。
_NEGATED_ACTION = re.compile(
    r"\b(?:don't|dont|do not|never)\b[^.,;!?\n]{0,24}"
    r"\b(?:do|cancel|change|modify|update|return|exchange|place|proceed|submit|confirm|go)\b"
)
# 这些短语本身就是拒绝，不需要再找动词
HARD_NEGATIVES = ("cancel that", "not yet", "hold off", "别", "不要", "先别")
# 裸词 "no" / "不" 只在**开头那一小句**里才算否定（理由见 is_affirmative）。
LEADING_NEGATIVES = ("no", "nope", "wait", "not", "不", "否")
POSITIVES = (
    "yes",
    "yep",
    "confirm",
    "go ahead",
    "please do",
    "correct",
    "ok",
    "okay",
    "sure",
    "确认",
    "对",
    "是的",
)

_CLAUSE_BREAK = re.compile(r"[,;.!?\n—–:]")


def is_affirmative(text: str) -> bool:
    """判定用户是否明确同意。

    **否定词只在开头那一小句里才算否定。** 这不是为了宽松，是被一次真实死循环
    逼出来的：模拟用户回复

        Confirmed — cancel #W5199551, "no longer needed."

    这是一句毫不含糊的同意，但里面引述的订单理由含有 "no longer"。原先用子串
    匹配 "no"，于是把它判成拒绝；确认永远拿不到，模型重发、护栏再拦，
    一题在同一步上循环 93 次直到撞上步骤上限，整题记 0 分。

    所以规则分三层：
      - 指向动作的否定（"don't cancel" / "do not proceed"）出现在任何位置都算否定
      - 现成的拒绝短语（cancel that / 别…）同样全句生效
      - 裸词 no / not / 不 只看开头那一小句——后面引述的内容不算表态

    第一层原先是裸的 "don't"，任务 80 上栽了一次：用户在一句谈论**语言**的话里
    用了 "don't"，却把同一条消息里的 "YES — go ahead!" 一起否决掉，交换没执行，
    整题 0 分。判否定的方向仍然从严——误判成拒绝只多一轮往返，误判成同意
    就是一次未经确认的写入。

    与应用侧 `agent._is_affirmative` 分开实现：基准里的模拟用户说英文，
    应用侧演示说中文。合并成一个函数会让两边的词表互相牵制。
    """
    lowered = text.strip().lower()
    if not lowered:
        return False
    if _NEGATED_ACTION.search(lowered):
        return False
    if any(word in lowered for word in HARD_NEGATIVES):
        return False

    head = _CLAUSE_BREAK.split(lowered, maxsplit=1)[0]
    head_words = set(re.findall(r"[a-z']+", head)) | set(head)
    if any(word in head_words for word in LEADING_NEGATIVES):
        return False

    return any(word in lowered for word in POSITIVES)


def build_agent_class() -> Any:
    """延迟构造 Agent 类。

    上游包在导入时就会读 `TAU2_DATA_DIR`，所以不能在模块顶层 import tau2——
    那会让任何只想用 `guardrails` 的调用方被迫先准备好数据目录。
    """
    from tau2.agent.llm_agent import LLMAgent, LLMAgentState
    from tau2.data_model.message import (
        AssistantMessage,
        MultiToolMessage,
        ToolMessage,
        UserMessage,
    )

    class GuardedState(LLMAgentState):  # type: ignore[misc]
        """上游状态 + 本项目的护栏状态。

        护栏状态挂在 state 上而不是 agent 实例上：批量运行是并发的，
        实例属性会让不同题互相污染，而污染的表现是"某几题莫名其妙通过了确认"。
        """

        model_config = ConfigDict(arbitrary_types_allowed=True)
        guard: Any = None

    class GuardedAgent(LLMAgent):  # type: ignore[misc]
        """上游 LLMAgent 外面套一层本项目的护栏。

        只覆写 `generate_next_message`：模型照常产出工具调用，我们在**发出之前**
        逐个裁决。不允许的调用不会被发出去，改为向用户说明。
        """

        def __init__(self, tools: Any, domain_policy: str, llm: str, llm_args: Any = None) -> None:
            super().__init__(tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args)
            self._known_tools = {t.name for t in tools}

        def get_init_state(self, message_history: Any = None) -> Any:
            base = super().get_init_state(message_history)
            return GuardedState(
                system_messages=base.system_messages,
                messages=base.messages,
                guard=GuardState(),
            )

        def generate_next_message(self, message: Any, state: Any) -> Any:
            guard: GuardState = state.guard

            # 1) 先从进来的消息里学东西：工具结果给身份与归属，用户答复消费确认
            for tool_message in _incoming_tool_messages(message, MultiToolMessage, ToolMessage):
                name = guard.call_names.get(tool_message.id)
                if name and tool_message.content and not tool_message.error:
                    observe_tool_result(guard, name, _clean(tool_message.content))
            if isinstance(message, UserMessage) and guard.pending is not None:
                if is_affirmative(message.content or ""):
                    guard.confirmed.add(guard.pending.fingerprint)
                guard.pending = None

            # 2) 让上游照常生成
            assistant = self._generate_next_message(message, state)

            # 3) 发出去之前逐个裁决
            calls = list(assistant.tool_calls or [])
            if not calls:
                state.messages.append(assistant)
                return assistant, state

            blocked: list[str] = []
            for call in calls:
                proposed = ProposedCall(call.name, dict(call.arguments or {}))
                verdict = review_call(guard, self._known_tools, proposed)
                if verdict.allowed:
                    continue
                blocked.append(verdict.reason)
                guard.interventions.append(
                    {"tool": call.name, "decision": str(verdict.decision), "reason": verdict.reason}
                )
                if verdict.decision is Decision.NEEDS_CONFIRMATION:
                    guard.pending = proposed

            if not blocked:
                for call in calls:
                    guard.call_names[call.id] = call.name
                state.messages.append(assistant)
                return assistant, state

            # 一次里只要有一个被拦下，整条消息都不发工具调用：
            # 上游要求一轮里要么发消息、要么发工具调用，不能两者兼有；
            # 而"放行一半"会让用户看到的解释与实际发生的事对不上。
            # **必须把 cost/usage/raw_data 原样带过来。** 模型那次调用真的发生过、
            # 真的花了钱，扣下工具调用不会退款。上游 `get_cost()` 更狠：整条轨迹里
            # 只要有一条 assistant 消息 cost 为 None，**整轮的 agent_cost 直接变成
            # None**，于是 M0.5 建立的「与 litellm 独立记账交叉校验」当场失效——
            # 而表现只是成本显示为 0，不报任何错。冒烟第一轮就踩了这个。
            held = AssistantMessage.text(
                "\n\n".join(blocked),
                cost=assistant.cost,
                usage=assistant.usage,
                raw_data=assistant.raw_data,
                generation_time_seconds=assistant.generation_time_seconds,
            )
            state.messages.append(held)
            return held, state

    return GuardedAgent


def _incoming_tool_messages(message: Any, multi_cls: Any, single_cls: Any) -> list[Any]:
    if isinstance(message, multi_cls):
        return list(message.tool_messages)
    if isinstance(message, single_cls):
        return [message]
    return []


def register(force: bool = False) -> str:
    """把带护栏的 Agent 注册进上游 registry，返回注册名。

    重复注册上游会抛错，所以这里做成幂等的——脚本可能被同一进程调用多次。
    """
    from tau2.registry import registry

    if registry.get_agent_factory(AGENT_NAME) is not None and not force:
        return AGENT_NAME

    agent_class = build_agent_class()

    def factory(tools: Any, domain_policy: str, llm: str, llm_args: Any = None, **_: Any) -> Any:
        # 工厂会被传入 task / audio 等我们用不到的参数，一律吞掉。
        return agent_class(tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args)

    registry.register_agent_factory(factory, AGENT_NAME)
    return AGENT_NAME
