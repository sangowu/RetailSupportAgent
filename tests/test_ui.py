"""Whether the UI can be constructed.

A real failure produced this test: `gr.Chatbot(type="messages")` raises
TypeError on gradio 6, which removed the parameter because messages is now
the only format. The UI process crashed on startup, and no test touched the
UI, so the broken page sat in the repository until
`scripts/start-local.ps1` first tried to launch it.

Construction calls no model and opens no port, so this test is offline, free
and fast.
"""

from __future__ import annotations

import pytest

from retail_support_agent.ui import build_demo


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Building the UI reads no key, because the agent is created lazily. The
    # placeholder is here so that if someone later moves Settings() into
    # construction, this test still fails only on real problems.
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")


def test_demo_builds() -> None:
    """If it constructs, every component argument is compatible with the
    installed gradio version."""
    demo = build_demo()
    assert demo is not None


def test_demo_is_not_shared_publicly() -> None:
    """M0 has no authentication, so the UI must never open a public share link.

    This inspects the source of `main()` rather than launching, which would
    bind a port and start a server. It catches someone enabling share for
    convenience.
    """
    import inspect

    from retail_support_agent import ui

    source = inspect.getsource(ui.main)
    assert "share=False" in source
    assert "127.0.0.1" in source


def test_every_handler_is_wired_to_the_same_outputs() -> None:
    """Gradio's own view of the wiring, at runtime (M7.2).

    The shapes of the update tuples are checked statically in test_ui_states.py; this
    checks the other half, that every event was actually given the same output list. A
    handler wired to fewer components silently leaves the rest of the page as it was,
    which for a sign-out is a conversation still on screen.
    """
    demo = build_demo()
    widths = {len(block.outputs) for block in demo.fns.values() if block.outputs}
    # The one exception is the follow-up that clears the password box, which writes to
    # that box alone and deliberately touches nothing else.
    assert widths == {1, 9}, widths

