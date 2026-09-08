"""Let tests import from the src layout without installing the package first."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Every credential and endpoint variable this project knows. Adding a field means
# adding it here too.
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
    """Clear real credentials before each test.

    This fixes an actual defect rather than being defensive. `tau2/utils/utils.py` calls
    `load_dotenv()` at import time, loading a developer's real keys from `.env` into
    `os.environ`. Once any test imports tau2, later tests in the same process using
    `Settings(_env_file=None)` read those real credentials, which both makes assertions
    depend on the machine's configuration and can print a real key into test output.

    Reproduced 2026-09-06: adding one test that imports tau2 made three previously
    passing endpoint tests read real keys and fail.

    Tests still set their own fake values; this only guarantees a clean starting point.
    """
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# The frozen simulated user (glm-5.3-flash) exists only on the Zhipu open platform, and
# `EvalStack` fails hard at construction when it would be sent elsewhere. A configured
# simulated-user endpoint is therefore the default starting point rather than a special
# case: otherwise every test constructing an EvalStack would hit that check and end up
# testing missing configuration instead of the behaviour under test. Tests that assert
# the failure remove these three themselves.
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
    """Supply the fake endpoint the frozen simulated user needs.

    Depends on `_isolate_credentials` for ordering: these must be written after the
    clearing step, or they would be removed again.
    """
    for name, value in USER_ENDPOINT_ENV.items():
        monkeypatch.setenv(name, value)
