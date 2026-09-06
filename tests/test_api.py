"""API 层测试：不触发模型调用，也不需要上游数据。

会话是懒创建的——只有真正 /chat 才会建领域环境，所以健康检查与入参校验
可以在没有 τ²-bench 数据、没有密钥余额的情况下离线验证。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from retail_support_agent.api import DEMO_USER_ID, create_app
from retail_support_agent.config import Settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
    return TestClient(create_app(settings=Settings()))  # type: ignore[call-arg]


def test_health_reports_milestone_and_demo_identity(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # 演示身份必须显式暴露，避免误以为这是真实用户系统
    assert body["demo_user_id"] == DEMO_USER_ID


def test_empty_message_is_rejected_before_reaching_the_model(client: TestClient) -> None:
    response = client.post("/chat", json={"session_id": "s1", "message": ""})
    assert response.status_code == 422


def test_oversized_message_is_rejected(client: TestClient) -> None:
    response = client.post("/chat", json={"session_id": "s1", "message": "x" * 5000})
    assert response.status_code == 422


def test_missing_session_id_is_rejected(client: TestClient) -> None:
    response = client.post("/chat", json={"message": "你好"})
    assert response.status_code == 422


def test_reset_is_idempotent_for_unknown_session(client: TestClient) -> None:
    response = client.post("/session/never-existed/reset")
    assert response.status_code == 200
    assert response.json()["status"] == "reset"


def test_demo_identity_is_a_simulated_user() -> None:
    # τ²-bench 模拟数据的 id 形如 <name>_<digits>，不是真实账户
    assert DEMO_USER_ID.endswith("_2033")
