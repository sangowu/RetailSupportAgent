"""The two commands an operator runs, and the decisions behind them.

Every decision this makes is a pure function of what AWS and the application reported, so
the cases that matter are testable without an instance: refusing to stop on somebody,
refusing to stop on silence, and not pretending a starting machine is a running one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

_spec = importlib.util.spec_from_file_location("demo", REPO / "scripts" / "demo.py")
assert _spec and _spec.loader
demo = importlib.util.module_from_spec(_spec)
sys.modules["demo"] = demo
_spec.loader.exec_module(demo)

Instance = demo.Instance
Busy = demo.Busy


def at(state: str, ip: str | None = "203.0.113.7") -> Instance:
    return Instance(id="i-0abc", state=state, public_ip=ip)


IDLE = Busy(reachable=True)
WORKING = Busy(reachable=True, holding=("1 write(s) waiting for confirmation",))
SILENT = Busy(reachable=False, detail="URLError: connection refused")


class TestStarting:
    def test_a_stopped_instance_is_started(self) -> None:
        assert demo.plan_start(at("stopped")).act

    def test_a_running_instance_is_left_alone(self) -> None:
        plan = demo.plan_start(at("running"))
        assert not plan.act
        assert plan.exit_code == 0, "already running is not an error"

    @pytest.mark.parametrize("state", ("pending", "stopping"))
    def test_a_machine_in_motion_is_not_pushed(self, state: str) -> None:
        """Sending start to a stopping instance is refused by EC2 anyway.

        Saying what is happening is more use than passing the error through.
        """
        plan = demo.plan_start(at(state))
        assert not plan.act
        assert state in plan.message

    def test_no_environment_says_so_and_points_somewhere(self) -> None:
        plan = demo.plan_start(None)
        assert not plan.act
        assert plan.exit_code == 1
        assert "terraform" in plan.message.lower()


class TestStopping:
    def test_an_idle_instance_is_stopped(self) -> None:
        assert demo.plan_stop(at("running"), IDLE, force=False).act

    def test_unfinished_work_stops_the_stop(self) -> None:
        """The case this exists for.

        A confirmation nobody has answered produces no traffic, so a demo that looks over
        is not. Stopping then loses the operator's place in someone else's conversation.
        """
        plan = demo.plan_stop(at("running"), WORKING, force=False)
        assert not plan.act
        assert "confirmation" in plan.message
        assert plan.exit_code == 2, "a refusal has its own code, distinct from a failure"

    def test_the_reason_is_named_rather_than_summarised(self) -> None:
        busy = Busy(
            reachable=True,
            holding=("2 approval(s) waiting for a human", "3 open ticket(s)"),
        )
        message = demo.plan_stop(at("running"), busy, force=False).message
        assert "approval" in message
        assert "ticket" in message

    def test_silence_is_not_permission(self) -> None:
        """An application that cannot answer might be mid-write.

        The operator has to look and say so; the instance's own uptime cap means this
        refusal cannot strand a machine indefinitely.
        """
        plan = demo.plan_stop(at("running"), SILENT, force=False)
        assert not plan.act
        assert "--force" in plan.message

    def test_force_overrides_and_says_what_it_overrode(self) -> None:
        plan = demo.plan_stop(at("running"), WORKING, force=True)
        assert plan.act
        assert "confirmation" in plan.message, "a silent force is a footgun"

    def test_force_works_when_the_application_never_answered(self) -> None:
        assert demo.plan_stop(at("running"), SILENT, force=True).act

    def test_an_already_stopped_instance_is_not_an_error(self) -> None:
        plan = demo.plan_stop(at("stopped", ip=None), None, force=False)
        assert not plan.act
        assert plan.exit_code == 0

    def test_a_machine_in_motion_is_left_alone(self) -> None:
        plan = demo.plan_stop(at("stopping"), None, force=False)
        assert not plan.act
        assert plan.exit_code == 1

    def test_no_environment_is_reported_not_crashed(self) -> None:
        assert not demo.plan_stop(None, None, force=False).act


class TestReadingTheApplication:
    def test_a_body_without_holding_reads_as_idle(self) -> None:
        assert demo.plan_stop(at("running"), Busy(reachable=True), force=False).act

    def test_a_failed_request_is_not_idle(self) -> None:
        assert not demo.plan_stop(at("running"), Busy(reachable=False), force=False).act


class TestUrls:
    def test_urls_are_built_from_the_current_address(self) -> None:
        instance = at("running", ip="198.51.100.9")
        assert instance.api_url == "http://198.51.100.9:8000"
        assert instance.ui_url == "http://198.51.100.9:7860"

    def test_a_stopped_instance_has_no_urls(self) -> None:
        """This shape uses no Elastic IP, so a stopped instance has no address at all.

        Returning a stale one would be worse than returning none.
        """
        instance = at("stopped", ip=None)
        assert instance.api_url is None
        assert instance.ui_url is None


class TestFindingTheInstance:
    def test_two_tagged_instances_are_refused_rather_than_guessed(self, monkeypatch) -> None:
        """Acting on an arbitrary one of two environments is the worst available answer."""

        def two(*_args: object, **_kwargs: object) -> dict:
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": "i-0aaa", "State": {"Name": "running"}},
                            {"InstanceId": "i-0bbb", "State": {"Name": "stopped"}},
                        ]
                    }
                ]
            }

        monkeypatch.setattr(demo, "aws", two)
        with pytest.raises(demo.DemoError) as caught:
            demo.find_instance("eu-west-1")
        assert "i-0aaa" in str(caught.value)
        assert "i-0bbb" in str(caught.value)

    def test_no_instances_is_none_rather_than_an_error(self, monkeypatch) -> None:
        monkeypatch.setattr(demo, "aws", lambda *a, **k: {"Reservations": []})
        assert demo.find_instance("eu-west-1") is None

    def test_terminated_instances_are_excluded_by_the_query(self, monkeypatch) -> None:
        """They keep their tags for a while, and a terminated instance is not an environment."""
        seen: dict[str, tuple] = {}

        def capture(*args: str, **kwargs: object) -> dict:
            seen["args"] = args
            return {"Reservations": []}

        monkeypatch.setattr(demo, "aws", capture)
        demo.find_instance("eu-west-1")
        states = [a for a in seen["args"] if a.startswith("Name=instance-state-name")]
        assert states and "terminated" not in states[0]
