"""The two commands an operator runs, and the decisions behind them.

Every decision this makes is a pure function of what AWS and the application reported, so
the cases that matter are testable without an instance: refusing to stop on somebody,
refusing to stop on silence, and not pretending a starting machine is a running one.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
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


def at(
    state: str,
    ip: str | None = "203.0.113.7",
    domain: str | None = "rsa.example.test",
    bucket: str | None = "rsa-demo-state",
) -> Instance:
    return Instance(id="i-0abc", state=state, public_ip=ip, domain=domain, bucket=bucket)


class Answer:
    """A stand-in for one urlopen call, used as a context manager the way urlopen is."""

    def __init__(self, body: dict[str, str]) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> Answer:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


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


class TestRestarting:
    """A restart is what a rollback is, so its refusals are the same as a stop's.

    Bringing the stack down loses a confirmation nobody has answered, whether the machine
    goes off afterwards or not.
    """

    def test_an_idle_instance_is_restarted(self) -> None:
        assert demo.plan_restart(at("running"), IDLE, force=False).act

    def test_unfinished_work_stops_the_restart(self) -> None:
        plan = demo.plan_restart(at("running"), WORKING, force=False)
        assert not plan.act
        assert "confirmation" in plan.message
        assert plan.exit_code == 2

    def test_silence_is_not_permission(self) -> None:
        plan = demo.plan_restart(at("running"), SILENT, force=False)
        assert not plan.act
        assert "--force" in plan.message

    def test_force_overrides_and_says_what_it_overrode(self) -> None:
        plan = demo.plan_restart(at("running"), WORKING, force=True)
        assert plan.act
        assert "confirmation" in plan.message

    def test_a_stopped_instance_is_pointed_at_start_instead(self) -> None:
        """Unlike a stop, this is not a no-op: the version is read at start, so starting
        it is what the operator wanted and saying so is more use than doing nothing."""
        plan = demo.plan_restart(at("stopped", ip=None), None, force=False)
        assert not plan.act
        assert "start" in plan.message
        assert plan.exit_code == 1

    def test_a_machine_in_motion_is_left_alone(self) -> None:
        assert not demo.plan_restart(at("pending"), None, force=False).act

    def test_no_environment_is_reported_not_crashed(self) -> None:
        assert not demo.plan_restart(None, None, force=False).act


class TestWhichVersionIsRunning:
    def test_an_environment_without_the_parameter_is_reported_not_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One built before M6.8 has no version parameter. Saying so beats a failure that
        reads like a broken tool."""

        def missing(*_args: object, **_kwargs: object) -> str:
            raise demo.DemoError("ParameterNotFound")

        monkeypatch.setattr(demo, "aws", missing)
        assert demo.configured_image("eu-west-1") is None

    def test_the_configured_version_is_read_from_one_place(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, tuple[str, ...]] = {}

        def capture(*args: str, **_kwargs: object) -> str:
            seen["args"] = args
            return "registry/rsa-app:m6.8"

        monkeypatch.setattr(demo, "aws", capture)
        assert demo.configured_image("eu-west-1") == "registry/rsa-app:m6.8"
        assert demo.IMAGE_PARAMETER in seen["args"]

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ({"status": "ok", "image": "registry/rsa-app:m6.8"}, "registry/rsa-app:m6.8"),
            # `/health` answers "unknown" when the setting is absent. A caller comparing
            # versions must not treat that string as one, or a restart would wait for a
            # version literally named unknown and time out.
            ({"status": "ok", "image": "unknown"}, None),
            ({"status": "ok"}, None),
        ],
    )
    def test_the_running_version_is_read_from_the_thing_that_is_running(
        self, monkeypatch: pytest.MonkeyPatch, body: dict[str, str], expected: str | None
    ) -> None:
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Answer(body))
        assert demo.running_image("https://rsa.example.test") == expected

    def test_a_service_that_does_not_answer_is_not_a_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_args: object, **_kwargs: object) -> object:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", refuse)
        assert demo.running_image("https://rsa.example.test") is None


class TestArchiving:
    """A dump blocks no writer, so being busy is not a reason to refuse one."""

    def test_a_running_instance_is_archived(self) -> None:
        assert demo.plan_archive(at("running")).act

    def test_being_busy_is_not_even_asked_about(self) -> None:
        """The opposite of a stop. Refusing here would refuse exactly when the history is
        worth most, and pg_dump reads one consistent snapshot without holding anyone up.

        Checked on the signature, because a `busy` argument this ignored today is one
        somebody starts honouring tomorrow."""
        assert list(inspect.signature(demo.plan_archive).parameters) == ["instance"]

    def test_a_stopped_instance_has_nothing_to_dump(self) -> None:
        plan = demo.plan_archive(at("stopped", ip=None))
        assert not plan.act
        assert "archived on its way down" in plan.message

    def test_an_instance_without_the_tag_has_nowhere_to_write(self) -> None:
        plan = demo.plan_archive(at("running", bucket=None))
        assert not plan.act
        assert "ArchiveBucket" in plan.message

    def test_no_environment_is_reported_not_crashed(self) -> None:
        assert not demo.plan_archive(None).act


class TestRestoring:
    """Restoring replaces the database the application is reading, so it refuses on work
    for the same reason a stop does, and one more: the work would be overwritten rather
    than merely lost."""

    def test_an_idle_instance_is_restored_onto(self) -> None:
        assert demo.plan_restore(at("running"), IDLE, force=False).act

    def test_unfinished_work_stops_the_restore(self) -> None:
        plan = demo.plan_restore(at("running"), WORKING, force=False)
        assert not plan.act
        assert "confirmation" in plan.message
        assert plan.exit_code == 2

    def test_silence_is_not_permission(self) -> None:
        plan = demo.plan_restore(at("running"), SILENT, force=False)
        assert not plan.act
        assert "--force" in plan.message

    def test_force_overrides_and_says_what_it_overrode(self) -> None:
        plan = demo.plan_restore(at("running"), WORKING, force=True)
        assert plan.act
        assert "confirmation" in plan.message

    def test_a_stopped_instance_is_pointed_at_start_first(self) -> None:
        plan = demo.plan_restore(at("stopped", ip=None), None, force=False)
        assert not plan.act
        assert "start" in plan.message

    @pytest.mark.parametrize(
        "given",
        [
            "archives/../../base/terraform.tfstate",
            "archives/x.sql.gz; rm -rf /",
            "archives/$(whoami).sql.gz",
            "/etc/passwd",
            "latest ; reboot",
            "",
        ],
    )
    def test_an_unexpected_key_is_refused_rather_than_run(self, given: str) -> None:
        """It is interpolated into a command that runs as root on the instance. An
        operator who pastes something unexpected should get a refusal, not a command."""
        with pytest.raises(demo.DemoError):
            demo.command_restore("eu-west-1", given, 1.0, force=False)

    @pytest.mark.parametrize("given", ["latest", "archives/rsa-20260910T101500Z.sql.gz"])
    def test_the_two_shapes_an_operator_types_are_accepted(
        self, given: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(demo, "find_instance", lambda _region: None)
        # Refused for having no environment, which is past the argument check.
        assert demo.command_restore("eu-west-1", given, 1.0, force=False) == 1


class TestStatusReportsTheArchive:
    def test_it_is_reported_for_a_stopped_instance_too(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A stopped instance is this environment's normal state, and the minute before a
        teardown is exactly when the age is worth reading. Found by running it: the first
        version returned early and printed nothing."""
        monkeypatch.setattr(demo, "find_instance", lambda _region: at("stopped", ip=None))
        monkeypatch.setattr(
            demo,
            "newest_archive",
            lambda _region, _bucket: demo.Archive(
                key="archives/rsa-20260910T104028Z.sql.gz",
                written=datetime.now(UTC) - timedelta(hours=3),
                size=3507,
            ),
        )
        assert demo.command_status("eu-west-1") == 0
        printed = capsys.readouterr().out
        assert "archives/rsa-20260910T104028Z.sql.gz" in printed
        assert "3h ago" in printed


class TestTheAgeOfTheNewestArchive:
    """The age is the only signal a silently failing archive gives. Nothing on the machine
    can report one: it is stopping when the archive runs."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(12, "12s ago"), (600, "10m ago"), (7200, "2h ago"), (259200, "3d ago")],
    )
    def test_it_is_said_in_the_unit_somebody_would_use(self, seconds: float, expected: str) -> None:
        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        archive = demo.Archive(
            key="archives/x.sql.gz", written=now - timedelta(seconds=seconds), size=1
        )
        assert archive.age(now) == expected

    def test_a_clock_that_disagrees_does_not_produce_a_negative_age(self) -> None:
        """S3's timestamp and this machine's clock are two clocks. An archive from the
        future should read as new, not as a negative number nobody can act on."""
        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        archive = demo.Archive(key="archives/x.sql.gz", written=now + timedelta(seconds=30), size=1)
        assert archive.age(now) == "0s ago"

    def test_an_environment_that_has_never_stopped_has_no_archive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(demo, "aws", lambda *a, **k: None)
        assert demo.newest_archive("eu-west-1", "a-bucket") is None

    def test_the_newest_is_read_by_the_query_not_by_this_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Listing every object and sorting here would page through a bucket that also
        holds the Terraform state."""
        seen: dict[str, tuple[str, ...]] = {}

        def capture(*args: str, **_kwargs: object) -> dict[str, object]:
            seen["args"] = args
            return {
                "Key": "archives/rsa-20260910T101500Z.sql.gz",
                "LastModified": "2026-09-10T10:15:00+00:00",
                "Size": 481920,
            }

        monkeypatch.setattr(demo, "aws", capture)
        archive = demo.newest_archive("eu-west-1", "a-bucket")
        assert archive is not None
        assert archive.size == 481920
        assert "sort_by(Contents, &LastModified)[-1]" in " ".join(seen["args"])
        assert demo.ARCHIVE_PREFIX in seen["args"]


class TestUrls:
    def test_the_url_is_the_name_over_tls(self) -> None:
        assert at("running", domain="rsa.example.test").url == "https://rsa.example.test"

    def test_the_url_does_not_change_when_the_address_does(self) -> None:
        """The point of the name.

        The address is different after every stop; a link someone was sent is not.
        """
        first = at("running", ip="198.51.100.9").url
        second = at("running", ip="203.0.113.44").url
        assert first == second

    def test_an_instance_without_the_tag_has_no_url(self) -> None:
        """It was built by older Terraform. Guessing a name would be worse than none."""
        assert at("running", domain=None).url is None


class TestFindingTheInstance:
    def test_the_domain_comes_from_the_instance_tag(self, monkeypatch) -> None:
        """One source of truth: the tool already finds the instance by tag."""

        def tagged(*_args: object, **_kwargs: object) -> dict:
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-0abc",
                                "State": {"Name": "running"},
                                "PublicIpAddress": "203.0.113.7",
                                "Tags": [{"Key": "Domain", "Value": "rsa.example.test"}],
                            }
                        ]
                    }
                ]
            }

        monkeypatch.setattr(demo, "aws", tagged)
        found = demo.find_instance("eu-west-1")
        assert found is not None
        assert found.url == "https://rsa.example.test"

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
