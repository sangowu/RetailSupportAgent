"""Start, stop, restart and inspect the AWS demo environment.

    uv run python scripts/demo.py status
    uv run python scripts/demo.py start
    uv run python scripts/demo.py stop
    uv run python scripts/demo.py restart
    uv run python scripts/demo.py archive
    uv run python scripts/demo.py restore latest

M6.4's Terraform builds the environment; this drives it. The instance is found by its tags
rather than by an id from Terraform's state, so this works from a machine that has not
initialised the backend, and so that a demo can be stopped by someone who is not the person
who applied it.

Two decisions are worth knowing about before reading:

**`start` waits for an answer, not for a state.** `running` means the hypervisor has the
machine. M6.2 measured 41 more seconds before the API and the UI reply, and a command that
returns early hands someone a broken link.

**`restart` is what a rollback is.** Which image runs is a Parameter Store value, not
something baked into the instance, so changing the version is `terraform apply` followed by
this. The stack comes down, the parameter is read again, and the new version comes up on
the same machine, the same volume and the same certificate.

**`stop` asks the application whether it is busy**, using the same verdict the instance's
own idle watcher uses (M6.3). An operator and the watcher disagreeing about "busy" would
mean one of them is wrong.

**`archive` is rarely the thing to run.** Every stop archives on its own way down, so this
is for the moment before a teardown, when the operator wants the last write in the archive
rather than in the volume they are about to delete. What this tool does add is the check:
`status` prints the newest archive and its age, and an age that keeps growing is the only
way a silently failing archive is ever noticed.

Reads credentials the way the AWS CLI does. No key is printed, and no key is needed: this
talks to EC2 and to the demo's own health routes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

PROJECT_TAG = "Project=RetailSupportAgent"
LAYER_TAG = "Layer=demo"
DEFAULT_REGION = "eu-west-1"

# Everything now goes through Caddy on one name. The application's own ports are not
# published at all, so /chat, /tickets and /approvals are unreachable from outside.
DEFAULT_SCHEME = "https"

# Which version the environment is configured to run. The instance reads this at every
# start; this tool reads it to say whether what is running is what was asked for.
IMAGE_PARAMETER = "/rsa/demo/image"

# What a restart runs on the instance. `rsa.service` is a oneshot whose ExecStop brings the
# stack down and whose ExecStart re-reads the parameter, so this is the whole of a rollback.
RESTART_COMMAND = "systemctl restart rsa.service"

# Where the database goes on the way down, and what puts it there and takes it back. The
# bucket is read from the instance's own tag, so this tool needs neither Terraform's state
# nor a second place that says which bucket this environment archives to.
ARCHIVE_PREFIX = "archives/"
ARCHIVE_COMMAND = "/usr/local/bin/rsa-archive"
RESTORE_COMMAND = "/usr/local/bin/rsa-restore"

# What may be passed to the restore script. It is interpolated into a shell command that
# runs as root on the instance, so it is checked here rather than trusted: an operator who
# pastes something unexpected should get a refusal, not a command.
ARCHIVE_KEY = re.compile(r"^archives/[A-Za-z0-9][A-Za-z0-9._-]*$")

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
    # The name is stable across stops; the address is not. The address is still carried
    # because it is what a stopped instance visibly lacks, and because a name that has not
    # caught up yet is a thing an operator needs to see.
    domain: str | None = None
    # Which bucket this environment's archives are in. Tagged rather than looked up, for
    # the same reason the domain is: one environment, one source of truth, found by tag.
    bucket: str | None = None

    @property
    def url(self) -> str | None:
        return f"{DEFAULT_SCHEME}://{self.domain}" if self.domain else None


@dataclass(frozen=True)
class Busy:
    """What the application says about whether it is in the middle of something."""

    reachable: bool
    holding: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class Archive:
    """One database archive in the bucket."""

    key: str
    written: datetime
    size: int

    def age(self, now: datetime) -> str:
        """How old it is, in the unit an operator would use out loud.

        The age, not the timestamp, is the thing being read. Whether the newest archive is
        four hours or four weeks old is the difference between an environment nobody has
        used and one whose archiving has quietly stopped, and a UTC timestamp makes the
        reader do that subtraction themselves.
        """
        seconds = max(0.0, (now - self.written).total_seconds())
        if seconds < 90:
            return f"{seconds:.0f}s ago"
        if seconds < 5400:
            return f"{seconds / 60:.0f}m ago"
        if seconds < 172800:
            return f"{seconds / 3600:.0f}h ago"
        return f"{seconds / 86400:.0f}d ago"


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
    tags = {str(t["Key"]): str(t["Value"]) for t in one.get("Tags", [])}
    return Instance(
        id=str(one["InstanceId"]),
        state=str(one["State"]["Name"]),
        public_ip=one.get("PublicIpAddress") or None,
        domain=tags.get("Domain") or None,
        bucket=tags.get("ArchiveBucket") or None,
    )


def instance_state(region: str, instance_id: str) -> str:
    data = aws("ec2", "describe-instances", "--instance-ids", instance_id, region=region)
    return str(data["Reservations"][0]["Instances"][0]["State"]["Name"])


def public_ip(region: str, instance_id: str) -> str | None:
    data = aws("ec2", "describe-instances", "--instance-ids", instance_id, region=region)
    return data["Reservations"][0]["Instances"][0].get("PublicIpAddress") or None


def configured_image(region: str) -> str | None:
    """Which version the environment is set to run, or None when nothing has set it.

    None rather than an error: an environment built before M6.8 has no such parameter, and
    saying so is more use than a failure that reads like a broken tool.
    """
    try:
        data = aws(
            "ssm",
            "get-parameter",
            "--name",
            IMAGE_PARAMETER,
            "--query",
            "Parameter.Value",
            region=region,
        )
    except DemoError:
        return None
    return str(data) if data else None


def newest_archive(region: str, bucket: str) -> Archive | None:
    """The most recent database archive, or None when nothing has been archived yet.

    None rather than an error, for the same reason as `configured_image`: an environment
    that has never stopped has no archive, and that is a fact rather than a fault.
    """
    try:
        data = aws(
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            ARCHIVE_PREFIX,
            "--query",
            "sort_by(Contents, &LastModified)[-1].{Key:Key,LastModified:LastModified,Size:Size}",
            region=region,
        )
    except DemoError:
        return None
    if not data or not data.get("Key"):
        return None
    return Archive(
        key=str(data["Key"]),
        written=datetime.fromisoformat(str(data["LastModified"]).replace("Z", "+00:00")),
        size=int(data.get("Size", 0)),
    )


def run_on_instance(region: str, instance_id: str, command: str, deadline: float) -> str:
    """Run one shell command on the instance and wait for it to finish.

    Through Systems Manager, so this needs no key on the machine and no port open to it.
    Waiting matters: the restart is what changes the version, and polling the application
    before it has begun would read the old one and call it done.

    Returns what the command printed, which is how `archive` and `restore` learn which
    object the instance actually wrote or read.
    """
    sent = aws(
        "ssm",
        "send-command",
        "--instance-ids",
        instance_id,
        "--document-name",
        "AWS-RunShellScript",
        "--parameters",
        json.dumps({"commands": [command]}),
        region=region,
    )
    command_id = str(sent["Command"]["CommandId"])
    started = time.monotonic()
    while True:
        time.sleep(POLL_SECONDS)
        got = aws(
            "ssm",
            "get-command-invocation",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
            region=region,
        )
        status = str(got.get("Status", ""))
        if status == "Success":
            return str(got.get("StandardOutputContent", "")).strip()
        if status in ("Cancelled", "TimedOut", "Failed"):
            detail = str(got.get("StandardErrorContent", "")).strip().splitlines()
            raise DemoError(
                f"{command} did not succeed on {instance_id} ({status}): "
                f"{detail[-1] if detail else 'no message'}"
            )
        if time.monotonic() - started > deadline:
            raise DemoError(f"{command} was still {status} after {deadline:.0f}s.")


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


def running_image(base_url: str) -> str | None:
    """Which image is answering, or None when it cannot be read.

    Read from the thing that is running rather than inferred from what was last applied.
    Without it a rollback is something someone believes happened.
    """
    try:
        with urllib.request.urlopen(
            f"{base_url}/health", timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    image = body.get("image")
    return str(image) if image and image != "unknown" else None


def ask_busy(base_url: str) -> Busy:
    """What the application says about being in the middle of something.

    Any failure is 'do not know', which section 1.3 of M6.5 treats as a reason not to stop
    on this tool's own initiative.
    """
    try:
        with urllib.request.urlopen(
            f"{base_url}/idle", timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
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


def plan_restart(instance: Instance | None, busy: Busy | None, *, force: bool) -> Plan:
    """Whether restarting now is allowed.

    The same rule as stopping, and for the same reason: a restart takes the stack down, so
    a confirmation nobody has answered is lost by it exactly as a stop would lose it. The
    difference is what a stopped instance means. Stopping one is a no-op; restarting one is
    a request the operator got wrong, because the version is read at start and starting it
    is what they want.
    """
    if instance is None:
        return Plan(False, "There is no demo environment.", 1)
    if instance.state == "stopped":
        return Plan(
            False,
            f"{instance.id} is stopped; `start` reads the configured version anyway.",
            1,
        )
    if instance.state not in STOPPABLE:
        return Plan(False, f"{instance.id} is {instance.state}; wait for it to settle", 1)

    if force:
        note = "; ".join(busy.holding) if busy and busy.holding else "nothing reported"
        return Plan(True, f"restarting {instance.id} anyway, overriding: {note}")
    if busy is None or not busy.reachable:
        detail = busy.detail if busy else "not asked"
        return Plan(
            False,
            f"{instance.id} is running but did not say whether it is busy ({detail}). "
            "Look at it, then use --force.",
            1,
        )
    if busy.holding:
        return Plan(False, "not restarting, work is unfinished: " + "; ".join(busy.holding), 2)
    return Plan(True, f"restarting {instance.id}")


def plan_archive(instance: Instance | None) -> Plan:
    """Whether archiving now makes sense.

    Nothing about being busy is asked. A dump is one consistent read and blocks no writer,
    so refusing to archive a busy demo would refuse exactly when the history is worth most.
    """
    if instance is None:
        return Plan(False, "There is no demo environment.", 1)
    if instance.state != "running":
        return Plan(
            False,
            f"{instance.id} is {instance.state}; there is nothing running to dump. "
            "The last stop archived on its way down.",
            1,
        )
    if not instance.bucket:
        return Plan(
            False,
            f"{instance.id} has no ArchiveBucket tag, so there is nowhere to write. "
            "Apply the current Terraform.",
            1,
        )
    return Plan(True, f"archiving the database on {instance.id}")


def plan_restore(instance: Instance | None, busy: Busy | None, *, force: bool) -> Plan:
    """Whether restoring now is allowed.

    The same rule as a stop and a restart, and one more reason than either: this replaces
    the database the application is reading. Work that a session is in the middle of would
    not merely be lost, it would be overwritten by a different account's history.
    """
    if instance is None:
        return Plan(False, "There is no demo environment.", 1)
    if instance.state != "running":
        return Plan(
            False,
            f"{instance.id} is {instance.state}; `start` it first, so there is a database "
            "to restore into.",
            1,
        )
    if not instance.bucket:
        return Plan(False, f"{instance.id} has no ArchiveBucket tag; apply the Terraform.", 1)

    if force:
        note = "; ".join(busy.holding) if busy and busy.holding else "nothing reported"
        return Plan(True, f"restoring onto {instance.id} anyway, overriding: {note}")
    if busy is None or not busy.reachable:
        detail = busy.detail if busy else "not asked"
        return Plan(
            False,
            f"{instance.id} is running but did not say whether it is busy ({detail}). "
            "Look at it, then use --force.",
            1,
        )
    if busy.holding:
        return Plan(False, "not restoring, work is unfinished: " + "; ".join(busy.holding), 2)
    return Plan(True, f"restoring onto {instance.id}")


# ---- the commands ----------------------------------------------------------


def wait_until_answering(
    region: str,
    instance_id: str,
    domain: str,
    deadline: float,
    expect_image: str | None = None,
) -> Instance:
    """Poll the name until the demo answers through it, or give up.

    The name, not the address: the instance rewrites its own A record at boot, and until
    that has happened and resolved, the demo is not reachable the way anyone will reach it.
    Waiting on the address instead would declare victory while the link in someone's
    calendar still pointed at last week's machine.
    """
    started = time.monotonic()
    url = f"{DEFAULT_SCHEME}://{domain}"
    while True:
        # During a restart the old containers keep answering until they are down, so
        # "it replies" is not "the new version is up". When a version is expected, that is
        # what is waited for.
        arrived = expect_image is None or running_image(url) == expect_image
        if arrived and answers(f"{url}/health", expect='"status":"ok"') and answers(f"{url}/"):
            return Instance(instance_id, "running", public_ip(region, instance_id), domain)
        waited = time.monotonic() - started
        if waited > deadline:
            raise DemoError(
                f"{domain} did not answer within {deadline:.0f}s. "
                "The instance may still be starting, the name may not have caught up, or "
                "the certificate may not have been issued yet; `status` again in a minute, "
                "or look at it with Session Manager."
            )
        time.sleep(POLL_SECONDS)


def command_status(region: str) -> int:
    instance = find_instance(region)
    if instance is None:
        print("no demo environment (nothing tagged Layer=demo)")
        return 0
    print(f"{instance.id}  {instance.state}")
    if instance.url:
        print(f"  {instance.url}")

    # Before the early return, deliberately. An archive is written on every stop and
    # nothing on the machine is left to report a failure, so an age that keeps growing
    # across stops is what a silently broken archive looks like from here - and a stopped
    # instance is this environment's normal state, so reporting it only while running
    # would hide it exactly when somebody is about to tear the environment down.
    if instance.bucket:
        archive = newest_archive(region, instance.bucket)
        if archive is None:
            print("  archive:    none yet; the next stop writes one")
        else:
            print(f"  archive:    {archive.key}  {archive.age(datetime.now(UTC))}")

    if instance.state != "running":
        return 0
    if not instance.url:
        print("  no Domain tag on the instance; it was not built by the current Terraform")
        return 1
    busy = ask_busy(instance.url)
    if not busy.reachable:
        print(f"  the application did not answer ({busy.detail})")
    elif busy.holding:
        print("  busy: " + "; ".join(busy.holding))
    else:
        print("  idle")

    # Configured and running are the same value almost always. They differ exactly when a
    # version has been applied and not yet picked up, which is the moment an operator needs
    # to be told about rather than left to infer.
    running = running_image(instance.url)
    configured = configured_image(region)
    print(f"  running:    {running or 'not reported'}")
    print(f"  configured: {configured or 'no version parameter; built before M6.8'}")
    if running and configured and running != configured:
        print("  the configured version is not the one running; `restart` picks it up")

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
    if not instance.domain:
        raise DemoError(
            f"{instance.id} has no Domain tag, so there is no name to wait on. "
            "Apply the current Terraform."
        )

    aws("ec2", "start-instances", "--instance-ids", instance.id, region=region)
    started = time.monotonic()
    ready = wait_until_answering(region, instance.id, instance.domain, deadline)
    print(f"ready in {time.monotonic() - started:.0f}s")
    print(f"  {ready.url}")
    print("It stops itself once nobody has used it for fifteen minutes.")
    return 0


def command_stop(region: str, *, force: bool) -> int:
    instance = find_instance(region)
    busy = ask_busy(instance.url) if instance and instance.url else None
    plan = plan_stop(instance, busy, force=force)
    print(plan.message)
    if not plan.act:
        return plan.exit_code

    assert instance is not None
    # A plain stop is a graceful one: EC2 sends the machine an ACPI shutdown, systemd stops
    # rsa.service, and its ExecStop archives the database and then runs `docker compose
    # down` so PostgreSQL closes its files. Nothing here has to stop the containers itself,
    # and nothing here has to archive: a stop that only this tool archived would leave the
    # idle watcher's own stops, which are most of them, with no archive at all.
    aws("ec2", "stop-instances", "--instance-ids", instance.id, region=region)
    print(
        "stopping; the database is archived on the way down, and the volume, the database "
        "and the image stay. Starting again takes about a minute."
    )
    return 0


def command_restart(region: str, deadline: float, *, force: bool) -> int:
    """Bring the stack down and up again on the same machine, reading the version afresh.

    This is what a rollback is. Nothing here decides which version to run: Terraform wrote
    that to a parameter, and the instance reads it. The volume, the database and the
    certificate are not involved.
    """
    instance = find_instance(region)
    busy = ask_busy(instance.url) if instance and instance.url else None
    plan = plan_restart(instance, busy, force=force)
    print(plan.message)
    if not plan.act:
        return plan.exit_code

    assert instance is not None and instance.domain
    target = configured_image(region)
    if target is None:
        raise DemoError(
            f"No version is configured at {IMAGE_PARAMETER}. "
            "Apply the current Terraform, which writes it."
        )
    print(f"  to {target}")

    started = time.monotonic()
    run_on_instance(region, instance.id, RESTART_COMMAND, deadline)
    ready = wait_until_answering(region, instance.id, instance.domain, deadline, target)
    print(f"answering {target} in {time.monotonic() - started:.0f}s")
    print(f"  {ready.url}")
    return 0


def command_archive(region: str, deadline: float) -> int:
    """Write the database to the bucket now, rather than waiting for the next stop.

    What this is for is the minute before a teardown: `archive`, check the age in
    `status`, then destroy.
    """
    instance = find_instance(region)
    plan = plan_archive(instance)
    print(plan.message)
    if not plan.act:
        return plan.exit_code

    assert instance is not None and instance.bucket
    started = time.monotonic()
    key = run_on_instance(region, instance.id, ARCHIVE_COMMAND, deadline)
    written = newest_archive(region, instance.bucket)
    print(
        f"wrote s3://{instance.bucket}/{key or ARCHIVE_PREFIX} in {time.monotonic() - started:.0f}s"
    )
    if written is not None:
        print(f"  {written.size} bytes, {written.age(datetime.now(UTC))}")
    return 0


def command_restore(region: str, archive: str, deadline: float, *, force: bool) -> int:
    """Put an archive back, on the instance that is running.

    The instance does the work: the dump never travels through this machine, and the
    bucket does not have to be readable from wherever an operator happens to be.
    """
    if archive != "latest" and not ARCHIVE_KEY.match(archive):
        raise DemoError(
            f"{archive!r} is not an archive key. Pass `latest`, or a key as `status` "
            f"prints it, beginning {ARCHIVE_PREFIX}."
        )

    instance = find_instance(region)
    busy = ask_busy(instance.url) if instance and instance.url else None
    plan = plan_restore(instance, busy, force=force)
    print(plan.message)
    if not plan.act:
        return plan.exit_code

    assert instance is not None and instance.domain
    print(f"  from {archive}")
    started = time.monotonic()
    key = run_on_instance(region, instance.id, f"{RESTORE_COMMAND} {archive}", deadline)
    # The API and the UI are stopped for the restore and started again by the script, so
    # this waits for the same thing a start waits for: the demo answering on its name.
    ready = wait_until_answering(region, instance.id, instance.domain, deadline)
    print(f"restored {key or archive} in {time.monotonic() - started:.0f}s")
    print(f"  {ready.url}")
    print("Everyone signed in was signed out: the UI holds its sessions in memory.")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Start, stop, restart, archive and inspect the AWS demo"
    )
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
    restart = sub.add_parser(
        "restart", help="re-read the configured version and bring the stack up on it"
    )
    restart.add_argument(
        "--force",
        action="store_true",
        help="restart even though the application reports work, or did not answer",
    )
    restart.add_argument(
        "--deadline",
        type=float,
        default=START_DEADLINE_SECONDS,
        help="seconds to wait for the new version to answer before giving up",
    )

    archive = sub.add_parser("archive", help="write the database to the bucket now")
    archive.add_argument(
        "--deadline",
        type=float,
        default=START_DEADLINE_SECONDS,
        help="seconds to wait for the dump before giving up",
    )
    restore = sub.add_parser("restore", help="put an archive back into the running database")
    restore.add_argument(
        "archive",
        help="`latest`, or an archive key as `status` prints it. Named rather than "
        "defaulted: this replaces the database.",
    )
    restore.add_argument(
        "--force",
        action="store_true",
        help="restore even though the application reports work, or did not answer",
    )
    restore.add_argument(
        "--deadline",
        type=float,
        default=START_DEADLINE_SECONDS,
        help="seconds to wait for the demo to answer again before giving up",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            return command_status(args.region)
        if args.command == "start":
            return command_start(args.region, args.deadline)
        if args.command == "restart":
            return command_restart(args.region, args.deadline, force=args.force)
        if args.command == "archive":
            return command_archive(args.region, args.deadline)
        if args.command == "restore":
            return command_restore(args.region, args.archive, args.deadline, force=args.force)
        return command_stop(args.region, force=args.force)
    except DemoError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
