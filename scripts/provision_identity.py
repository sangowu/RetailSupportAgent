"""Map an authenticated subject to a retail identity, and grant or revoke its roles.

Provisioning is an administrative act. There is deliberately no HTTP route to it: a
service that lets a caller create its own account and pick its own role has no
authorization model at all, only a form. This script is the whole admin interface, and
running it requires the database URL, which a client never has.

Usage:
    uv run python scripts/provision_identity.py --subject <sub> --user-id <retail id> \
        --role customer
    uv run python scripts/provision_identity.py --subject <sub> --show
    uv run python scripts/provision_identity.py --subject <sub> --revoke supervisor

`--subject` is the `sub` claim of the token the identity provider issues (for Cognito,
the user pool's immutable subject, not the username, which can be reused after deletion).
Nothing here prints a token, a key or the database password.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_support_agent.auth import ROLE_PERMISSIONS
from retail_support_agent.config import Settings
from retail_support_agent.db import make_engine, make_session_factory
from retail_support_agent.store import Store


def open_store() -> Store:
    settings = Settings()  # type: ignore[call-arg]
    if settings.database_url is None:
        raise SystemExit("RSA_DATABASE_URL is not set; identities live in the database.")
    engine = make_engine(settings.database_url.get_secret_value())
    return Store(make_session_factory(engine))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True, help="the token's sub claim")
    parser.add_argument("--user-id", help="the retail identity that owns the orders")
    parser.add_argument(
        "--role",
        action="append",
        default=[],
        choices=sorted(ROLE_PERMISSIONS),
        help="role to grant; may be repeated",
    )
    parser.add_argument("--revoke", action="append", default=[], help="role to remove")
    parser.add_argument("--show", action="store_true", help="print the identity and stop")
    args = parser.parse_args()

    store = open_store()

    # Changes first, then the report. Printing before applying them would show the state
    # the caller asked to change, which is exactly the state they are checking against.
    if args.user_id:
        store.provision_identity(args.subject, args.user_id, args.role)
    elif args.role:
        existing = store.identity_for(args.subject)
        if existing is None:
            raise SystemExit("--user-id is required the first time a subject is provisioned.")
        store.provision_identity(args.subject, existing.user_id, args.role)

    for role in args.revoke:
        store.revoke_role(args.subject, role)

    identity = store.identity_for(args.subject)
    if identity is None:
        print(f"{args.subject}: not provisioned")
        return 1
    roles = ", ".join(sorted(identity.roles)) or "(none)"
    print(f"{args.subject} -> {identity.user_id}, roles: {roles}")
    if args.show:
        granted = ", ".join(sorted(p.value for p in identity.permissions)) or "(none)"
        print(f"  permissions: {granted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
