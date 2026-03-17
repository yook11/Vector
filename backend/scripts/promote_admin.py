"""CLI script to promote/demote a user to/from admin role.

Usage:
    # Promote to admin
    python scripts/promote_admin.py --email admin@example.com

    # Demote to user
    python scripts/promote_admin.py --email admin@example.com --demote

    # Docker environment
    docker compose exec api python scripts/promote_admin.py --email admin@example.com
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure the backend package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine


async def promote_or_demote(email: str, *, demote: bool = False) -> None:
    target_role = "user" if demote else "admin"
    action = "Demoting" if demote else "Promoting"

    async with engine.connect() as conn:
        result = await conn.execute(
            text('SELECT id, role FROM auth."user" WHERE email = :email'),
            {"email": email},
        )
        row = result.one_or_none()

        if row is None:
            print(f"Error: User with email '{email}' not found.")
            sys.exit(1)

        old_role = row.role
        if old_role == target_role:
            print(f"User '{email}' already has role '{target_role}'.")
            return

        await conn.execute(
            text('UPDATE auth."user" SET role = :role WHERE email = :email'),
            {"role": target_role, "email": email},
        )
        await conn.commit()

        print(f"{action} user '{email}': {old_role} -> {target_role}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote or demote a user role")
    parser.add_argument("--email", required=True, help="Email of the user to modify")
    parser.add_argument(
        "--demote",
        action="store_true",
        default=False,
        help="Demote user from admin to regular user",
    )
    args = parser.parse_args()

    asyncio.run(promote_or_demote(args.email, demote=args.demote))


if __name__ == "__main__":
    main()
