"""界面能不能构造出来。

这条测试是被一次真实故障逼出来的：`gr.Chatbot(type="messages")` 在 gradio 6 上
直接 TypeError（该参数已被删除，messages 成了唯一格式）。界面进程起来就崩，
而**没有任何测试碰过界面**，所以这个坏了的界面一直躺在仓库里，直到
`scripts/start-local.ps1` 第一次真的去启动它才暴露。

构造不调模型，也不监听端口，所以这条测试离线、免费、够快。
"""

from __future__ import annotations

import pytest

from retail_support_agent.ui import build_demo


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # 构造界面本身不读密钥（Agent 是懒创建的），设一个占位值是为了
    # 万一将来有人把 Settings() 提到构造期，这条测试仍然只失败在真正的问题上。
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")


def test_demo_builds() -> None:
    """能构造出来，就说明所有组件参数与当前 gradio 版本兼容。"""
    demo = build_demo()
    assert demo is not None


def test_demo_is_not_shared_publicly() -> None:
    """M0 没有认证，界面绝不能开公开分享链接。

    盯着 `main()` 的源码而不是真去 launch：启动会占端口、拉起服务器。
    这条能拦住的是"有人为了图方便把 share 打开"。
    """
    import inspect

    from retail_support_agent import ui

    source = inspect.getsource(ui.main)
    assert "share=False" in source
    assert "127.0.0.1" in source
