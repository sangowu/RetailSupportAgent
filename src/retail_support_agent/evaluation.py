"""M0.4 评测运行：把上游 τ²-bench 的编排器与评分器接到 DashScope 上。

不重写上游的编排、模拟用户与评分逻辑，只做两件事：
1. 把 litellm 的模型路由指向 DashScope 的 OpenAI 兼容端点；
2. 把 NL 断言评判模型从上游默认的 gpt-4.1 换成我们能访问的**固定**模型。

评测栈的冻结纪律（AGENTS.md）：模拟用户与评判模型一旦选定就不再变动，
且**不从候选池里选**——否则等于让候选模型给自己打分。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from retail_support_agent.config import CANDIDATE_MODELS, Settings

# 冻结的评测栈。二者都不在候选池内，且用带日期的快照 ID 而非别名，
# 避免服务端在别名后面悄悄换模型导致历史结果不可比。
FROZEN_USER_SIMULATOR = "qwen3.6-plus-2026-04-02"
FROZEN_NL_JUDGE = "qwen3.6-plus-2026-04-02"

# 评测栈一律 temperature=0，与上游默认一致，减少评分本身的波动。
FROZEN_STACK_TEMPERATURE = 0.0


class EvaluationSetupError(RuntimeError):
    """评测栈配置不满足冻结要求。"""


def _assert_not_candidate(model_id: str, role: str) -> None:
    if model_id in CANDIDATE_MODELS:
        raise EvaluationSetupError(
            f"{role} 使用了候选模型 {model_id}：会造成自评偏袒，必须换成候选池外的模型"
        )


def litellm_model(model_id: str) -> str:
    """DashScope 的 OpenAI 兼容端点在 litellm 里走 openai/ 前缀。"""
    return f"openai/{model_id}"


@dataclass
class EvalStack:
    """一次评测运行的完整模型配置。写入运行 manifest 后即冻结。"""

    settings: Settings
    agent_model: str
    user_model: str = FROZEN_USER_SIMULATOR
    judge_model: str = FROZEN_NL_JUDGE
    agent_extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _assert_not_candidate(self.user_model, "模拟用户")
        _assert_not_candidate(self.judge_model, "NL 断言评判模型")

    def _routing(self) -> dict[str, Any]:
        return {
            "api_base": self.settings.base_url,
            "api_key": self.settings.dashscope_api_key.get_secret_value(),
        }

    def agent_args(self) -> dict[str, Any]:
        return {**self._routing(), **self.agent_extra}

    def frozen_args(self) -> dict[str, Any]:
        return {**self._routing(), "temperature": FROZEN_STACK_TEMPERATURE}

    def describe(self) -> dict[str, Any]:
        """给运行 manifest 用的描述。**不含密钥**。"""
        return {
            "agent_model": self.agent_model,
            "agent_extra": self.agent_extra,
            "user_simulator": self.user_model,
            "nl_judge": self.judge_model,
            "frozen_stack_temperature": FROZEN_STACK_TEMPERATURE,
            "base_url": self.settings.base_url,
        }


def prepare_environment(data_dir: str) -> None:
    """必须在导入 tau2 之前调用。"""
    os.environ["TAU2_DATA_DIR"] = data_dir


def patch_nl_judge(stack: EvalStack) -> None:
    """把 NL 断言评判模型换成固定的可访问模型。

    上游在模块导入时就把默认值绑进了 evaluator 模块的全局名字，
    改 tau2.config 已经来不及，必须直接改那个模块的全局变量。
    """
    from tau2.evaluator import evaluator_nl_assertions as nl

    nl.DEFAULT_LLM_NL_ASSERTIONS = litellm_model(stack.judge_model)
    nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS = stack.frozen_args()


def build_run_config(
    stack: EvalStack,
    task_ids: list[str],
    num_trials: int,
    max_concurrency: int,
    seed: int,
    save_to: str | None,
) -> Any:
    """构造上游的运行配置。任务集与划分一律取自 M0.1 冻结的清单。"""
    from tau2.data_model.simulation import TextRunConfig

    return TextRunConfig(
        domain="retail",
        task_ids=task_ids,
        task_split_name=None,  # 任务由 task_ids 精确指定，不再二次筛选
        agent="llm_agent",
        llm_agent=litellm_model(stack.agent_model),
        llm_args_agent=stack.agent_args(),
        user="user_simulator",
        llm_user=litellm_model(stack.user_model),
        llm_args_user=stack.frozen_args(),
        num_trials=num_trials,
        max_concurrency=max_concurrency,
        seed=seed,
        save_to=save_to,
        log_level="ERROR",
    )
