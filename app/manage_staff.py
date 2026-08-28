from __future__ import annotations

import argparse
import asyncio
import getpass
import os

import bcrypt

from app.staff_repository import staff_repository
from app.support_auth import normalize_staff_email


ROLES = ("agent", "supervisor", "admin")


async def _create(args: argparse.Namespace) -> None:
    password = os.environ.get("STAFF_INITIAL_PASSWORD")
    if not password:
        password = getpass.getpass("Staff password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise SystemExit("Passwords do not match.")
    if len(password) < 12:
        raise SystemExit("Staff passwords must be at least 12 characters.")

    normalized = normalize_staff_email(args.email)
    if not normalized or "@" not in normalized:
        raise SystemExit("A valid staff email is required.")

    existing = await staff_repository.get_staff_by_email(normalized)
    if existing:
        raise SystemExit("A staff account with that email already exists.")

    password_hash = await asyncio.to_thread(
        bcrypt.hashpw,
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=12),
    )
    staff_id = await staff_repository.create_staff(
        name=args.name.strip(),
        email=args.email.strip(),
        email_normalized=normalized,
        password_hash=password_hash.decode("utf-8"),
        role=args.role,
    )
    print(f"Created staff account id={staff_id} role={args.role}")


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Manage support staff accounts.")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Create the first or an additional staff account.")
    create.add_argument("--name", required=True)
    create.add_argument("--email", required=True)
    create.add_argument("--role", choices=ROLES, default="agent")
    args = parser.parse_args()

    try:
        if args.command == "create":
            await _create(args)
    finally:
        await staff_repository.close()


if __name__ == "__main__":
    asyncio.run(_main())
