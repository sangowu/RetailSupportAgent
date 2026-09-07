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


class TestResetAllSessions:
    """`scripts/reset-demo.ps1` 依赖的重置入口。

    每个会话各持一份内存里的数据库副本，所以"重置演示"就是丢掉会话——
    磁盘上的评测结果、基线和 .env 一概不碰。
    """

    def test_reset_all_reports_how_many_were_cleared(self, client: TestClient) -> None:
        """0 条和 N 条都是成功，但对操作者是两件事，必须能区分。"""
        assert client.post("/sessions/reset").json()["cleared"] == 0

        # 用被拒绝的入参建不出会话，所以直接问 registry 太绕；
        # 这里用 /session/<id>/reset 的幂等性反证"没有会话"这一初始状态。
        assert client.post("/session/s1/reset").status_code == 200
        assert client.post("/sessions/reset").json()["cleared"] == 0

    def test_reset_all_declares_its_scope(self, client: TestClient) -> None:
        """范围要写在响应里：操作者据此确认自己没有误删评测结果。"""
        body = client.post("/sessions/reset").json()
        assert body["status"] == "reset"
        assert body["scope"] == "all_sessions"

    def test_health_exposes_active_session_count(self, client: TestClient) -> None:
        """停服脚本据此判断"现在关掉会不会打断正在进行的演示"。"""
        assert client.get("/health").json()["active_sessions"] == 0
