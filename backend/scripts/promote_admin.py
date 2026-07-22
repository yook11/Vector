"""既存ユーザーを operator 経路から昇格・降格する CLI。

Usage:
    # Promote to admin
    python scripts/promote_admin.py --email admin@example.com

    # Demote to user
    python scripts/promote_admin.py --email admin@example.com --demote

    # Docker environment
    docker compose exec backend python scripts/promote_admin.py \\
        --email admin@example.com
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import NoReturn

# Ensure the backend package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.config import settings
from app.db_ssl import create_app_engine


def _exit_with_error(message: str) -> NoReturn:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


async def promote_or_demote(email: str, *, demote: bool = False) -> None:
    """既存ユーザー1件の role だけを認証保守用接続で変更する。"""
    auth_database_url = settings.auth_retention_database_url
    if auth_database_url is None:
        _exit_with_error("AUTH_RETENTION_DATABASE_URL is not configured.")

    normalized_email = email.strip().lower()
    target_role = "user" if demote else "admin"
    action = "Demoted" if demote else "Promoted"
    engine = None

    try:
        engine = create_app_engine(auth_database_url)
        async with engine.connect() as conn:
            result = await conn.execute(
                text('SELECT id, role FROM auth."user" WHERE email = :email'),
                {"email": normalized_email},
            )
            row = result.one_or_none()

            if row is None:
                _exit_with_error("user not found.")

            if row.role == target_role:
                print(f"User already has role '{target_role}'; no changes made.")
                return

            update_result = await conn.execute(
                text('UPDATE auth."user" SET role = :role WHERE id = :user_id'),
                {"role": target_role, "user_id": row.id},
            )
            if update_result.rowcount != 1:
                _exit_with_error("database operation failed.")
            await conn.commit()
    except SystemExit:
        raise
    except Exception:
        _exit_with_error("database operation failed.")
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:
                print("Warning: database cleanup failed.", file=sys.stderr)

    print(f"{action} user to role '{target_role}'.")


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
