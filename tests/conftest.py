"""让测试直接从 src 布局导入，无需先安装本包。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 本项目认识的全部凭证与端点变量。新增字段时必须同步加进来。
CREDENTIAL_ENV_VARS = (
    "DASHSCOPE_API_KEY",
    "RSA_OPENAI_BASE_URL",
    "RSA_JUDGE_BASE_URL",
    "RSA_JUDGE_API_KEY",
    "RSA_JUDGE_MODEL",
    "RSA_AGENT_BASE_URL",
    "RSA_AGENT_API_KEY",
    "RSA_USER_BASE_URL",
    "RSA_USER_API_KEY",
    "RSA_USER_MODEL",
)


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试开始前清掉真实凭证。

    不是防御性洁癖，是修一个已经发生的缺陷：`tau2/utils/utils.py` 在
    **导入时**就执行 `load_dotenv()`，把开发机 `.env` 的真实 Key 灌进
    `os.environ`。于是只要某个测试导入了 tau2，同一进程里后面用
    `Settings(_env_file=None)` 的测试就会读到真实凭证——
    结果既让断言随开发机配置而变，也可能把真实 Key 打进测试输出。

    2026-09-06 实测复现：新增一个导入 tau2 的测试后，三个原本通过的
    端点测试开始读到真实 Key 并失败。

    各测试仍自行 setenv 它需要的假值；这里只负责保证起点是干净的。
    """
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# 冻结的模拟用户（glm-5.3-flash）只在智谱开放平台上，`EvalStack` 构造时会硬失败
# 拦截"发错平台"。所以"配好了模拟用户端点"是测试的**默认起点**，而不是特例：
# 否则每个构造 EvalStack 的测试都会撞上那条校验，测的就成了缺配置而非被测行为。
# 需要断言"没配就报错"的测试自行 delenv 这三项。
FAKE_USER_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
FAKE_USER_KEY = "sk-zhipu-2222222222"
FAKE_USER_MODEL = "glm-5.3-flash"

USER_ENDPOINT_ENV = {
    "RSA_USER_BASE_URL": FAKE_USER_BASE_URL,
    "RSA_USER_API_KEY": FAKE_USER_KEY,
    "RSA_USER_MODEL": FAKE_USER_MODEL,
}


@pytest.fixture(autouse=True)
def _frozen_user_endpoint(_isolate_credentials: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """给测试补上冻结模拟用户所需的端点（假值）。

    依赖 `_isolate_credentials` 保证顺序：必须在清空之后再写入，
    否则会被清掉。
    """
    for name, value in USER_ENDPOINT_ENV.items():
        monkeypatch.setenv(name, value)
