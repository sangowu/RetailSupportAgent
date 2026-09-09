"""List what this project has running in AWS, and what it has cost this month.

The operations contract requires that after a demo is stopped, the remaining resources
are listed rather than assumed gone. This is that list. It is read-only on purpose:
deleting is Terraform's job, and a script that can delete is a script that can delete the
wrong thing.

Everything this project creates carries `Project=RetailSupportAgent`, so the tagging API
answers the question in one call per region. Anything created without that tag will not
appear here, which is the one way this report can be wrong; the tag is therefore applied
at creation and not afterwards.

A terminated EC2 instance keeps its tags for a while, and so does a deleted volume. Those
are listed separately as gone rather than dropped: the safe direction for a report
answering "is anything left" is to show too much, but a dozen dead instances above one live
one answers nothing. Anything whose state cannot be established counts as still there.

Reads credentials the way the AWS CLI does. No key is printed, and the account id is
shown masked.

Usage:
    uv run python scripts/aws_inventory.py
    uv run python scripts/aws_inventory.py --region eu-west-1 --region us-east-1
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any

PROJECT_TAG_KEY = "Project"
PROJECT_TAG_VALUE = "RetailSupportAgent"
DEFAULT_REGIONS = ("eu-west-1",)


def aws(*args: str) -> Any:
    """One CLI call, returning parsed JSON, or None when the call fails.

    A failure is reported rather than raised: a missing permission for one service must
    not hide the resources this can see.
    """
    try:
        done = subprocess.run(
            ["aws", *args, "--output", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  [aws cli unavailable] {exc}", file=sys.stderr)
        return None
    if done.returncode != 0:
        first = (done.stderr or "").strip().splitlines()
        print(f"  [failed] aws {' '.join(args[:2])}: {first[0] if first else 'no message'}")
        return None
    return json.loads(done.stdout) if done.stdout.strip() else None


def masked_account() -> str:
    identity = aws("sts", "get-caller-identity")
    if not identity:
        return "unknown"
    account = str(identity.get("Account", ""))
    return f"{account[:4]}****{account[-2:]}" if len(account) > 6 else "unknown"


def tagged_resources(region: str) -> list[dict[str, Any]]:
    """Every resource in this region carrying the project tag, with its tags."""
    found: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        args = [
            "resourcegroupstaggingapi",
            "get-resources",
            "--region",
            region,
            "--tag-filters",
            f"Key={PROJECT_TAG_KEY},Values={PROJECT_TAG_VALUE}",
        ]
        if token:
            args += ["--pagination-token", token]
        page = aws(*args)
        if not page:
            break
        found.extend(page.get("ResourceTagMappingList", []))
        token = page.get("PaginationToken") or None
        if not token:
            break
    return found


def month_to_date_cost() -> list[tuple[str, float]]:
    """Cost by service since the first of the month.

    Cost Explorer lags by up to a day, so a resource created minutes ago will not show
    here. It answers "what has this account been charged", not "what is running".
    """
    today = datetime.now(UTC).date()
    start = today.replace(day=1)
    end = today + timedelta(days=1)
    data = aws(
        "ce",
        "get-cost-and-usage",
        "--time-period",
        f"Start={start.isoformat()},End={end.isoformat()}",
        "--granularity",
        "MONTHLY",
        "--metrics",
        "UnblendedCost",
        "--group-by",
        "Type=DIMENSION,Key=SERVICE",
    )
    if not data:
        return []
    rows: list[tuple[str, float]] = []
    for period in data.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amount > 0.001:
                rows.append((str(group["Keys"][0]), amount))
    return sorted(rows, key=lambda row: -row[1])


def masked_arn(arn: str) -> str:
    """The same ARN with the account id masked.

    This output is meant to be pasted into a report or a pull request. The account id is
    not a secret, but it is an identifier of the user's account and nothing here needs
    it: region, service and resource name are what identify a leftover.
    """
    parts = arn.split(":")
    if len(parts) > 4 and parts[4].isdigit() and len(parts[4]) > 6:
        parts[4] = f"{parts[4][:4]}****"
    return ":".join(parts)


def service_of(arn: str) -> str:
    parts = arn.split(":")
    return parts[2] if len(parts) > 2 else "?"


def resource_id(arn: str) -> str:
    """The last path segment, which is the id for every EC2 resource type here."""
    return arn.rsplit("/", 1)[-1]


def layer_of(entry: dict[str, Any]) -> str:
    """Which Terraform layer owns this, from its tags.

    The base layer is meant to survive a teardown, so listing it beside a leftover demo
    resource without saying which is which turns a clean destroy into a false alarm.
    """
    for tag in entry.get("Tags", []):
        if tag.get("Key") == "Layer":
            return str(tag.get("Value", "?"))
    return "no layer tag"


GONE_STATES = {"terminated", "deleted", "shutting-down", "deleting"}

# EC2 resource kinds this resolves. Each entry is the describe call, the filter name it
# accepts, and where the id and the state live in the response.
#
# Filters rather than --instance-ids or --volume-ids: those fail the whole call when one
# id no longer exists, which is precisely the case this is here to detect. Measured: one
# deleted volume made every volume read as unknown.
_KINDS: dict[str, tuple[str, str, str, str, str]] = {
    "instance": (
        "describe-instances",
        "instance-id",
        "Reservations[].Instances[]",
        "InstanceId",
        "State.Name",
    ),
    "volume": ("describe-volumes", "volume-id", "Volumes[]", "VolumeId", "State"),
    "security-group": ("describe-security-groups", "group-id", "SecurityGroups[]", "GroupId", ""),
    "security-group-rule": (
        "describe-security-group-rules",
        "security-group-rule-id",
        "SecurityGroupRules[]",
        "SecurityGroupRuleId",
        "",
    ),
}


def _dig(item: dict[str, Any], path: str) -> Any:
    for part in path.split("."):
        item = item.get(part, {}) if isinstance(item, dict) else {}
    return item


def _flatten(data: Any, path: str) -> list[dict[str, Any]]:
    """Walk a JMESPath-shaped list expression like Reservations[].Instances[]."""
    items: list[Any] = [data]
    for part in path.split("[]"):
        key = part.strip(".")
        if not key:
            continue
        nxt: list[Any] = []
        for item in items:
            value = item.get(key, []) if isinstance(item, dict) else []
            nxt.extend(value if isinstance(value, list) else [value])
        items = nxt
    return [i for i in items if isinstance(i, dict)]


def _ec2_states(region: str, kind: str, ids: list[str]) -> dict[str, str]:
    """State per id, for one EC2 resource kind. An id not returned no longer exists."""
    if not ids:
        return {}
    call, filter_name, list_path, id_field, state_path = _KINDS[kind]
    data = aws(
        "ec2", call, "--region", region, "--filters", f"Name={filter_name},Values={','.join(ids)}"
    )
    if not data:
        return {}
    found: dict[str, str] = {}
    for item in _flatten(data, list_path):
        identifier = item.get(id_field)
        if not identifier:
            continue
        # An existing resource with no state field of its own is simply there.
        found[str(identifier)] = str(_dig(item, state_path)) if state_path else "exists"
    return found


def liveness(region: str, arns: list[str]) -> dict[str, str]:
    """ "live, <state>", "gone" or "unknown" per ARN.

    Only the EC2 kinds in _KINDS are resolved: those are the ones that keep their tags
    after they stop existing. Everything else is reported as the tagging API returned it.
    """
    by_kind: dict[str, list[str]] = {kind: [] for kind in _KINDS}
    for arn in arns:
        for kind in _KINDS:
            if f":{kind}/" in arn:
                by_kind[kind].append(resource_id(arn))
                break

    states: dict[str, str] = {}
    for kind, ids in by_kind.items():
        states.update(_ec2_states(region, kind, ids))

    verdicts: dict[str, str] = {}
    for arn in arns:
        if not any(f":{kind}/" in arn for kind in _KINDS):
            verdicts[arn] = "unknown"
            continue
        state = states.get(resource_id(arn))
        verdicts[arn] = "gone" if state is None or state in GONE_STATES else f"live, {state}"
    return verdicts


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="List this project's AWS resources and spend")
    parser.add_argument("--region", action="append", default=None)
    args = parser.parse_args()
    regions = tuple(args.region) if args.region else DEFAULT_REGIONS

    print(f"account {masked_account()}    {date.today().isoformat()}")

    total_live = 0
    for region in regions:
        entries = tagged_resources(region)
        arns = [str(e["ResourceARN"]) for e in entries]
        verdicts = liveness(region, arns)
        layers = {str(e["ResourceARN"]): layer_of(e) for e in entries}

        live = [a for a in arns if verdicts[a] != "gone"]
        gone = [a for a in arns if verdicts[a] == "gone"]
        total_live += len(live)

        print(f"\n{region}: {len(live)} live, {len(gone)} tagged but gone")
        by_layer: dict[str, list[str]] = {}
        for arn in live:
            by_layer.setdefault(layers[arn], []).append(arn)
        for layer, items in sorted(by_layer.items()):
            print(f"  {layer}")
            for arn in sorted(items, key=service_of):
                note = verdicts[arn]
                suffix = "" if note == "unknown" else f"  [{note}]"
                print(f"      {service_of(arn):<8}{masked_arn(arn)}{suffix}")
        if gone:
            print(
                f"  gone, still tagged: {len(gone)} (a terminated instance keeps its tags a while)"
            )

    if total_live == 0:
        print("\nNothing of this project's exists. Untagged resources would not appear here.")

    rows = month_to_date_cost()
    if rows:
        print("\nAccount spend this month, by service (the whole account, not only this project)")
        for service, amount in rows:
            print(f"  {amount:8.2f} USD  {service}")
        print(f"  {sum(a for _, a in rows):8.2f} USD  total")
        print("  Cost Explorer lags by up to a day; a resource created minutes ago is not here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
