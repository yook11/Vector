"""E2E 用の seed user/admin を auth.user / auth.account に投入する。

frontend Phase 3 PR #249 で導入した Playwright E2E (`frontend/e2e/`) は
`POST /api/auth/sign-in/email` で programmatic login する設計のため、
事前に DB 上に test user が存在する必要がある。

Better Auth の signUp API は `role: "admin"` を `input: false` で受け付けない
(frontend `src/lib/auth/auth.ts` の additionalFields 定義)ため、admin user は
DB 直接 INSERT が必須。一貫性のため user/admin 両方とも本 migration で seed する。

password hash は Better Auth (`@better-auth/utils/password.node.mjs`) と互換な
Python `hashlib.scrypt` で計算する。互換性は実機 signUp との完全一致で検証済み:
  - password を `unicodedata.normalize("NFKC", ...)` で正規化
  - salt は 32 文字の hex 文字列を **bytes 化せずそのまま ASCII エンコード** (Node の
    `crypto.scrypt(password, saltString, ...)` は文字列を UTF-8 として扱うため)
  - params: N=16384, r=16, p=1, dkLen=64, maxmem=128*N*r*2

UUID は固定値を hard-code する。Python 3.13 の `uuid.uuid7()` は使わない: 毎回
違う値だと `alembic downgrade -1 && upgrade` で別 row が増える可能性があるため。

**本 migration は本番 DB に投入してはならない**。E2E 専用 user を本番に置くと
`Password123!` で signIn を試行可能になる。production deploy 時には手動で
`alembic downgrade -1` するか、deploy script で本 revision をスキップする運用と
する (本 PR の範囲外)。

Revision ID: i4_seed_e2e_users
Revises: g3c4d5e6f8a9
Create Date: 2026-04-29
"""

from __future__ import annotations

import datetime
import hashlib
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "i4_seed_e2e_users"
down_revision: str | None = "g3c4d5e6f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Better Auth `@better-auth/utils/password.node.mjs` 既定値と一致させる
_SCRYPT_N = 16384
_SCRYPT_R = 16
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2  # 64 MiB

# E2E 専用なので暗号学的 random でなくて良い (固定値で再現性を確保)
_USER_SALT_HEX = "00112233445566778899aabbccddeeff"
_ADMIN_SALT_HEX = "ffeeddccbbaa99887766554433221100"

# UUID v7 形式の固定値 (13 桁目=7, 17 桁目=a で variant 0b10)
_USER_ID = "01900000-0000-7000-a000-00000000e2e1"
_ADMIN_ID = "01900000-0000-7000-a000-00000000e2e2"
_USER_ACCOUNT_ID = "01900000-0000-7000-a000-00000000ac01"
_ADMIN_ACCOUNT_ID = "01900000-0000-7000-a000-00000000ac02"

_USER_EMAIL = "e2e@example.com"
_ADMIN_EMAIL = "e2e-admin@example.com"

# `frontend/e2e/fixtures/users.ts` の値と必ず一致させること
_PASSWORD = "Password123!"  # noqa: S105 — E2E test fixture password


def _better_auth_scrypt_hash(password: str, salt_hex: str) -> str:
    """Better Auth 互換の `${salt_hex}:${key_hex}` を返す。

    Node の `crypto.scrypt(password, saltString, ...)` は salt を UTF-8 文字列の
    bytes として扱うため、Python 側でも hex 文字列を ASCII でエンコードしてから
    渡す (decode した 16 bytes ではない)。
    """
    pw = unicodedata.normalize("NFKC", password).encode("utf-8")
    salt = salt_hex.encode("ascii")
    key = hashlib.scrypt(
        pw,
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"{salt_hex}:{key.hex()}"


def upgrade() -> None:
    user_table = sa.table(
        "user",
        sa.column("id", postgresql.UUID(as_uuid=False)),
        sa.column("name", sa.Text),
        sa.column("email", sa.Text),
        sa.column("emailVerified", sa.Boolean),
        sa.column("createdAt", sa.DateTime(timezone=True)),
        sa.column("updatedAt", sa.DateTime(timezone=True)),
        sa.column("role", sa.Text),
        schema="auth",
    )
    account_table = sa.table(
        "account",
        sa.column("id", postgresql.UUID(as_uuid=False)),
        sa.column("accountId", sa.Text),
        sa.column("providerId", sa.Text),
        sa.column("userId", postgresql.UUID(as_uuid=False)),
        sa.column("password", sa.Text),
        sa.column("createdAt", sa.DateTime(timezone=True)),
        sa.column("updatedAt", sa.DateTime(timezone=True)),
        schema="auth",
    )

    now = datetime.datetime.now(datetime.UTC)

    op.bulk_insert(
        user_table,
        [
            {
                "id": _USER_ID,
                "name": "E2E User",
                "email": _USER_EMAIL,
                "emailVerified": True,
                "createdAt": now,
                "updatedAt": now,
                "role": "user",
            },
            {
                "id": _ADMIN_ID,
                "name": "E2E Admin",
                "email": _ADMIN_EMAIL,
                "emailVerified": True,
                "createdAt": now,
                "updatedAt": now,
                "role": "admin",
            },
        ],
    )
    op.bulk_insert(
        account_table,
        [
            {
                "id": _USER_ACCOUNT_ID,
                "accountId": _USER_ID,
                "providerId": "credential",
                "userId": _USER_ID,
                "password": _better_auth_scrypt_hash(_PASSWORD, _USER_SALT_HEX),
                "createdAt": now,
                "updatedAt": now,
            },
            {
                "id": _ADMIN_ACCOUNT_ID,
                "accountId": _ADMIN_ID,
                "providerId": "credential",
                "userId": _ADMIN_ID,
                "password": _better_auth_scrypt_hash(_PASSWORD, _ADMIN_SALT_HEX),
                "createdAt": now,
                "updatedAt": now,
            },
        ],
    )


def downgrade() -> None:
    # auth.account.userId は uuid 型なので明示的に cast する
    op.execute(
        sa.text(
            'DELETE FROM auth.account WHERE "userId" IN '
            "(CAST(:user_id AS uuid), CAST(:admin_id AS uuid))"
        ).bindparams(user_id=_USER_ID, admin_id=_ADMIN_ID)
    )
    op.execute(
        sa.text(
            'DELETE FROM auth."user" WHERE email IN (:user_email, :admin_email)'
        ).bindparams(user_email=_USER_EMAIL, admin_email=_ADMIN_EMAIL)
    )
