"""List what this project has running in AWS, and what it has cost this month.

The operations contract requires that after a demo is stopped, the remaining resources
are listed rather than assumed gone. This is that list. It is read-only on purpose:
deleting is Terraform's job, and a script that can delete is a script that can delete the
wrong thing.

Everything this project creates carries `Project=RetailSupportAgent`, so the tagging API
answers the question in one call per region. Anything created without that tag will not
appear here, which is the one way this report can be wrong; the tag is therefore applied
at creation and not afterwards.

The report errs towards over-reporting, on purpose. A terminated EC2 instance keeps its
tags for a while and so keeps appearing here after it has stopped being billed. Reading a
leftover that is already gone wastes a minute; missing one that is still billed does not.

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


def tagged_resources(region: str) -> list[str]:
    """Every resource in this region carrying the project tag."""
    found: list[str] = []
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
        found.extend(str(r["ResourceARN"]) for r in page.get("ResourceTagMappingList", []))
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


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="List this project's AWS resources and spend")
    parser.add_argument("--region", action="append", default=None)
    args = parser.parse_args()
    regions = tuple(args.region) if args.region else DEFAULT_REGIONS

    print(f"account {masked_account()}    {date.today().isoformat()}")

    total = 0
    for region in regions:
        arns = tagged_resources(region)
        total += len(arns)
        print(f"\n{region}: {len(arns)} resources tagged {PROJECT_TAG_KEY}={PROJECT_TAG_VALUE}")
        by_service: dict[str, list[str]] = {}
        for arn in arns:
            by_service.setdefault(service_of(arn), []).append(arn)
        for service, items in sorted(by_service.items()):
            print(f"  {service:<24}{len(items)}")
            for arn in items:
                print(f"      {masked_arn(arn)}")

    if total == 0:
        print("\nNothing of this project's is running. Untagged resources would not appear here.")

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
