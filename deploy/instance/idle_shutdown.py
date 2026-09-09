"""Stop the demo instance once nobody is using it.

This runs on the instance, not in the container, because it ends by stopping the machine
it is running on. It asks the application whether it is idle rather than measuring CPU: a
request blocked on a model call uses almost none, and a confirmation nobody has answered
looks like an empty machine from outside.

Standard library only, and the decision is a pure function, so the behaviour that matters
can be tested without an instance. The two parts that cannot be are the call to
`shutdown` and the timer that runs this.

Configuration, all through the environment:

    RSA_IDLE_URL              default http://127.0.0.1:8000/idle
    RSA_IDLE_MINUTES          shut down after this much continuous idleness, default 15
    RSA_MAX_UPTIME_MINUTES    shut down after this long whatever the answer, default 240
    RSA_IDLE_COMPOSE_FILE     the stack to stop first, default /opt/rsa/compose.yaml
    RSA_IDLE_DRY_RUN          set to 1 to log the decision and change nothing

Usage:
    python3 idle_shutdown.py           # decide once and act; a timer runs it every minute
    python3 idle_shutdown.py --check   # print the decision and exit, changing nothing
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8000/idle"
DEFAULT_IDLE_MINUTES = 15.0
DEFAULT_MAX_UPTIME_MINUTES = 240.0
DEFAULT_COMPOSE_FILE = "/opt/rsa/compose.yaml"
# A module constant so a test can point it somewhere that does not exist.
UPTIME_PATH = "/proc/uptime"
REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Answer:
    """What the application said, or that it said nothing.

    `reachable` is false when the request failed for any reason. That is deliberately not
    modelled as a kind of failure: from here, a connection refused and a 500 mean the same
    thing, which is that this process does not know whether anyone is working.
    """

    reachable: bool
    idle_seconds: float = 0.0
    holding: tuple[str, ...] = ()
    detail: str = ""

    @classmethod
    def unreachable(cls, detail: str) -> Answer:
        return cls(reachable=False, detail=detail)


@dataclass(frozen=True)
class Decision:
    shutdown: bool
    reason: str


def ask(url: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> Answer:
    """Ask the application whether it is idle. Any failure is 'do not know'."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return Answer.unreachable(f"{type(exc).__name__}: {exc}")
    return Answer(
        reachable=True,
        idle_seconds=float(body.get("idle_seconds", 0.0)),
        holding=tuple(str(h) for h in body.get("holding", [])),
    )


def decide(
    answer: Answer,
    uptime_seconds: float,
    *,
    idle_seconds_required: float,
    max_uptime_seconds: float,
) -> Decision:
    """Whether to stop the machine, and why.

    Two independent reasons, and the order matters. The cap is checked first because its
    whole purpose is to fire when the first reason cannot: an application that never
    answers, or one that answers "busy" forever because a session is stuck.

    An unreachable application is never idle. A watcher that shut down when it could not
    get an answer would shut down hardest exactly when something is wrong and someone is
    looking at it.
    """
    if uptime_seconds >= max_uptime_seconds:
        return Decision(True, f"up for {uptime_seconds / 60:.0f} minutes, past the cap")
    if not answer.reachable:
        return Decision(False, f"the application did not answer ({answer.detail})")
    if answer.holding:
        return Decision(False, "; ".join(answer.holding))
    if answer.idle_seconds >= idle_seconds_required:
        return Decision(True, f"idle for {answer.idle_seconds / 60:.0f} minutes")
    remaining = (idle_seconds_required - answer.idle_seconds) / 60
    return Decision(False, f"idle, {remaining:.0f} more minutes to go")


def uptime_seconds() -> float:
    """Seconds since this machine booted.

    /proc/uptime rather than the process's own clock: the watcher is restarted by a timer,
    so its own age says nothing about how long the instance has been billed.
    """
    try:
        with Path(UPTIME_PATH).open(encoding="ascii") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        # Without an uptime the cap cannot fire. Report zero, which makes this process
        # rely on the application's answer alone rather than shutting down on a bad read.
        return 0.0


def stop_stack(compose_file: str) -> None:
    """Stop the containers before the machine.

    PostgreSQL gets to close its files. Pulling the power from underneath it does not
    corrupt it, but it does force a recovery on the next start, and the next start is the
    number M6.1 chose this shape for.
    """
    subprocess.run(
        ["/usr/bin/docker", "compose", "-f", compose_file, "down"],
        check=False,
        timeout=180,
    )


def power_off() -> None:
    subprocess.run(["/usr/sbin/shutdown", "-h", "now"], check=False, timeout=30)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"{name}={raw!r} is not a number; using {default}", file=sys.stderr)
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop this instance when nobody is using it")
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the decision and exit without changing anything",
    )
    args = parser.parse_args(argv)

    url = os.getenv("RSA_IDLE_URL", DEFAULT_URL)
    answer = ask(url)
    decision = decide(
        answer,
        uptime_seconds(),
        idle_seconds_required=_float_env("RSA_IDLE_MINUTES", DEFAULT_IDLE_MINUTES) * 60,
        max_uptime_seconds=_float_env("RSA_MAX_UPTIME_MINUTES", DEFAULT_MAX_UPTIME_MINUTES) * 60,
    )

    verb = "stopping" if decision.shutdown else "staying up"
    print(f"{verb}: {decision.reason}", flush=True)

    if args.check or not decision.shutdown:
        return 0
    if os.getenv("RSA_IDLE_DRY_RUN", "") == "1":
        print("dry run: nothing stopped", flush=True)
        return 0

    stop_stack(os.getenv("RSA_IDLE_COMPOSE_FILE", DEFAULT_COMPOSE_FILE))
    power_off()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
