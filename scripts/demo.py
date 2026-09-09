"""Start, stop and inspect the AWS demo environment.

Three commands for the two things an operator does, plus the one they do first.

    uv run python scripts/demo.py status
    uv run python scripts/demo.py start
    uv run python scripts/demo.py stop

M6.4's Terraform builds the environment; this drives it. The instance is found by its tags
rather than by an id from Terraform's state, so this works from a machine that has not
initialised the backend, and so that a demo can be stopped by someone who is not the person
who applied it.

Two decisions are worth knowing about before reading:

**`start` waits for an answer, not for a state.** `running` means the hypervisor has the
machine. M6.2 measured 41 more seconds before the API and the UI reply, and a command that
returns early hands someone a broken link.

**`stop` asks the application whether it is busy**, using the same verdict the instance's
own idle watcher uses (M6.3). An operator and the watcher disagreeing about "busy" would
mean one of them is wrong.

Reads credentials the way the AWS CLI does. No key is printed, and no key is needed: this
talks to EC2 and to the demo's own health routes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

PROJECT_TAG = "Project=RetailSupportAgent"
LAYER_TAG = "Layer=demo"
DEFAULT_REGION = "eu-west-1"

API_PORT = 8000
UI_PORT = 7860

# M6.1's rule is 120 seconds from the start command to a page that answers. This is the
# patience before giving up, not the expectation: M6.2 measured 41 seconds.
START_DEADLINE_SECONDS = 300.0
POLL_SECONDS = 3.0
REQUEST_TIMEOUT_SECONDS = 5.0

# States from which a start or a stop makes sense. Anything else is a machine in motion,
# and the right thing is to say so rather than to send a request that will be refused.
STARTABLE = {"stopped"}
STOPPABLE = {"running"}


class DemoError(RuntimeError):
    """Something the operator has to know about, reported without a traceback."""


@dataclass(frozen=True)
class Instance:
    id: str
    state: str
    public_ip: str | None

    @property
    def api_url(self) -> str | None:
        return f"http://{self.public_ip}:{API_PORT}" if self.public_ip else None

    @property
    def ui_url(self) -> str | None:
        return f"http://{self.public_ip}:{UI_PORT}" if self.public_ip else None


@dataclass(frozen=True)
class Busy:
    """What the application says about whether it is in the middle of something."""

    reachable: bool
    holding: tuple[str, ...] = ()
    detail: str = ""


# ---- talking to AWS --------------------------------------------------------


def aws(*args: str, region: str) -> Any:
    """One CLI call, returning parsed JSON.

    Shelling out rather than taking a boto3 dependency, for the reason
    scripts/aws_inventory.py gives: this is an operator's tool and it should use the
    credentials, profile and region resolution the operator already has configured.
    """
    done = subprocess.run(
        ["aws", *args, "--region", region, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if done.returncode != 0:
        first = (done.stderr or "").strip().splitlines()
        raise DemoError(f"aws {' '.join(args[:2])} failed: {first[-1] if first else 'no message'}")
    return json.loads(done.stdout) if done.stdout.strip() else None


def find_instance(region: str) -> Instance | None:
    """The demo instance, by tag. None when there is no environment.

    Terminated instances are excluded: they keep their tags for a while, and a terminated
    instance is not an environment.
    """
    data = aws(
        "ec2",
        "describe-instances",
        "--filters",
        f"Name=tag:{PROJECT_TAG.split('=')[0]},Values={PROJECT_TAG.split('=')[1]}",
        f"Name=tag:{LAYER_TAG.split('=')[0]},Values={LAYER_TAG.split('=')[1]}",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
        region=region,
    )
    found = [i for r in (data or {}).get("Reservations", []) for i in r.get("Instances", [])]
    if not found:
        return None
    if len(found) > 1:
        ids = ", ".join(sorted(str(i["InstanceId"]) for i in found))
        raise DemoError(
            f"{len(found)} instances carry the demo tags ({ids}). "
            "This tool acts on one environment; remove the extra one or narrow the tags."
        )
    one = found[0]
    return Instance(
        id=str(one["InstanceId"]),
        state=str(one["State"]["Name"]),
        public_ip=one.get("PublicIpAddress") or None,
    )


def instance_state(region: str, instance_id: str) -> str:
    data = aws("ec2", "describe-instances", "--instance-ids", instance_id, region=region)
    return str(data["Reservations"][0]["Instances"][0]["State"]["Name"])


def public_ip(region: str, instance_id: str) -> str | None:
    data = aws("ec2", "describe-instances", "--instance-ids", instance_id, region=region)
    return data["Reservations"][0]["Instances"][0].get("PublicIpAddress") or None


# ---- talking to the demo ---------------------------------------------------


def answers(url: str, expect: str | None = None) -> bool:
    """Whether a URL replies, optionally with something expected in the body."""
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return False
            body = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return expect is None or expect in body


def ask_busy(api_url: str) -> Busy:
    """What the application says about being in the middle of something.

    Any failure is 'do not know', which section 1.3 of M6.5 treats as a reason not to stop
    on this tool's own initiative.
    """
    try:
        with urllib.request.urlopen(f"{api_url}/idle", timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return Busy(reachable=False, detail=f"{type(exc).__name__}: {exc}")
    return Busy(reachable=True, holding=tuple(str(h) for h in body.get("holding", [])))


# ---- the decisions, which are the part worth testing -----------------------


@dataclass(frozen=True)
class Plan:
    act: bool
    message: str
    exit_code: int = 0


def plan_start(instance: Instance | None) -> Plan:
    if instance is None:
        return Plan(
            False,
            "There is no demo environment. Create one: see deploy/terraform/README.md.",
            1,
        )
    if instance.state in STARTABLE:
        return Plan(True, f"starting {instance.id}")
    if instance.state == "running":
        return Plan(False, f"{instance.id} is already running")
    return Plan(False, f"{instance.id} is {instance.state}; wait for it to settle", 1)


def plan_stop(instance: Instance | None, busy: Busy | None, *, force: bool) -> Plan:
    """Whether stopping now is allowed.

    An unreachable application does not permit a stop on its own: the operator has to say
    so with --force, having looked. The instance's own uptime cap still applies whatever is
    decided here, so nothing can be stranded indefinitely by this refusal.
    """
    if instance is None:
        return Plan(False, "There is no demo environment.", 1)
    if instance.state == "stopped":
        return Plan(False, f"{instance.id} is already stopped")
    if instance.state not in STOPPABLE:
        return Plan(False, f"{instance.id} is {instance.state}; wait for it to settle", 1)

    if force:
        note = "; ".join(busy.holding) if busy and busy.holding else "nothing reported"
        return Plan(True, f"stopping {instance.id} anyway, overriding: {note}")
    if busy is None or not busy.reachable:
        detail = busy.detail if busy else "not asked"
        return Plan(
            False,
            f"{instance.id} is running but did not say whether it is busy ({detail}). "
            "Look at it, then use --force.",
            1,
        )
    if busy.holding:
        return Plan(False, "not stopping, work is unfinished: " + "; ".join(busy.holding), 2)
    return Plan(True, f"stopping {instance.id}")


# ---- the commands ----------------------------------------------------------


def wait_until_answering(region: str, instance_id: str, deadline: float) -> Instance:
    """Poll until both ports reply, or give up.

    The address is read on every pass rather than once: it is assigned during the start and
    is not there when the first call is made.
    """
    started = time.monotonic()
    while True:
        address = public_ip(region, instance_id)
        if address:
            api = f"http://{address}:{API_PORT}"
            if answers(f"{api}/health", expect='"status":"ok"') and answers(
                f"http://{address}:{UI_PORT}/"
            ):
                return Instance(instance_id, "running", address)
        waited = time.monotonic() - started
        if waited > deadline:
            raise DemoError(
                f"{instance_id} did not answer within {deadline:.0f}s. "
                "It may still be starting; `status` again in a minute, "
                "or look at it with Session Manager."
            )
        time.sleep(POLL_SECONDS)


def command_status(region: str) -> int:
    instance = find_instance(region)
    if instance is None:
        print("no demo environment (nothing tagged Layer=demo)")
        return 0
    print(f"{instance.id}  {instance.state}")
    if instance.state != "running" or not instance.api_url:
        return 0
    print(f"  api  {instance.api_url}")
    print(f"  ui   {instance.ui_url}")
    busy = ask_busy(instance.api_url)
    if not busy.reachable:
        print(f"  the application did not answer ({busy.detail})")
    elif busy.holding:
        print("  busy: " + "; ".join(busy.holding))
    else:
        print("  idle")
    return 0


def command_start(region: str, deadline: float) -> int:
    instance = find_instance(region)
    plan = plan_start(instance)
    print(plan.message)
    if not plan.act:
        if instance is not None and instance.state == "running":
            return command_status(region)
        return plan.exit_code

    assert instance is not None
    aws("ec2", "start-instances", "--instance-ids", instance.id, region=region)
    started = time.monotonic()
    ready = wait_until_answering(region, instance.id, deadline)
    print(f"ready in {time.monotonic() - started:.0f}s")
    print(f"  api  {ready.api_url}")
    print(f"  ui   {ready.ui_url}")
    print("It stops itself once nobody has used it for fifteen minutes.")
    return 0


def command_stop(region: str, *, force: bool) -> int:
    instance = find_instance(region)
    busy = ask_busy(instance.api_url) if instance and instance.api_url else None
    plan = plan_stop(instance, busy, force=force)
    print(plan.message)
    if not plan.act:
        return plan.exit_code

    assert instance is not None
    # A plain stop is a graceful one: EC2 sends the machine an ACPI shutdown, systemd stops
    # rsa.service, and its ExecStop runs `docker compose down` so PostgreSQL closes its
    # files. Nothing here has to stop the containers itself.
    aws("ec2", "stop-instances", "--instance-ids", instance.id, region=region)
    print(
        "stopping; the volume, the database and the image stay. "
        "Starting again takes about a minute."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Start, stop and inspect the AWS demo")
    parser.add_argument("--region", default=DEFAULT_REGION)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="where the environment is, and whether it is busy")
    start = sub.add_parser("start", help="start it and wait until it answers")
    start.add_argument(
        "--deadline",
        type=float,
        default=START_DEADLINE_SECONDS,
        help="seconds to wait for an answer before giving up",
    )
    stop = sub.add_parser("stop", help="stop it, unless work is unfinished")
    stop.add_argument(
        "--force",
        action="store_true",
        help="stop even though the application reports work, or did not answer",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            return command_status(args.region)
        if args.command == "start":
            return command_start(args.region, args.deadline)
        return command_stop(args.region, force=args.force)
    except DemoError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
