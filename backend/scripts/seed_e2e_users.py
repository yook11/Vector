"""E2E test 用の seed user/admin を auth.user / auth.account に投入する。

red-team C2 chain (production E2E seed admin) を構造的に塞ぐため、本処理は
alembic migration から外して CI/E2E 専用 script に分離した。production deploy
で `alembic upgrade head` が走っても seed user は作られない。

Usage:
    python scripts/seed_e2e_users.py

Env (override 可、未設定時は CI default):
    E2E_SEED_USER_EMAIL    (default: e2e@example.com)
    E2E_SEED_USER_PASSWORD (default: Password123!)
    E2E_SEED_ADMIN_EMAIL   (default: e2e-admin@example.com)
    E2E_SEED_ADMIN_PASSWORD (default: Password123!)

idempotent: ON CONFLICT (id) DO NOTHING で安全に再実行できる。固定 UUID を
持つ fixture user なので id 一致 = email 一致が保証される。

production ガード: ENV=production の環境では即 sys.exit(2) で中断する
(運用者が誤って打つリスクへの最後の防御線)。i4_seed_e2e_users migration を
削除した PR4 (red-team C2 防御) で導入。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import os
import sys
import unicodedata
from pathlib import Path

# scripts/ から backend package を import 可能にする (既存 promote_admin.py
# と同じパターン)。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db_ssl import create_app_engine  # noqa: E402

# Better Auth `@better-auth/utils/password.node.mjs` 既定値と一致させる。
# i4_seed_e2e_users.py から移植 (互換性は実機 signUp との完全一致で検証済み)。
_SCRYPT_N = 16384
_SCRYPT_R = 16
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2  # 64 MiB

# E2E 専用なので暗号学的 random でなくて良い (固定値で再現性を確保)。
_USER_SALT_HEX = "00112233445566778899aabbccddeeff"
_ADMIN_SALT_HEX = "ffeeddccbbaa99887766554433221100"

# UUID v7 形式の固定値 (13 桁目=7, 17 桁目=a で variant 0b10)。
_USER_ID = "01900000-0000-7000-a000-00000000e2e1"
_ADMIN_ID = "01900000-0000-7000-a000-00000000e2e2"
_USER_ACCOUNT_ID = "01900000-0000-7000-a000-00000000ac01"
_ADMIN_ACCOUNT_ID = "01900000-0000-7000-a000-00000000ac02"

_DEFAULT_USER_EMAIL = "e2e@example.com"
_DEFAULT_ADMIN_EMAIL = "e2e-admin@example.com"
_DEFAULT_PASSWORD = "Password123!"  # noqa: S105 — E2E test fixture password


def _better_auth_scrypt_hash(password: str, salt_hex: str) -> str:
    """Better Auth 互換の `${salt_hex}:${key_hex}` を返す。

    Node の `crypto.scrypt(password, saltString, ...)` は salt を UTF-8 文字列
    の bytes として扱うため、Python 側でも hex 文字列を ASCII でエンコードして
    から渡す (decode した 16 bytes ではない)。
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


async def _seed() -> None:
    user_email = os.environ.get("E2E_SEED_USER_EMAIL", _DEFAULT_USER_EMAIL)
    user_pw = os.environ.get("E2E_SEED_USER_PASSWORD", _DEFAULT_PASSWORD)
    admin_email = os.environ.get("E2E_SEED_ADMIN_EMAIL", _DEFAULT_ADMIN_EMAIL)
    admin_pw = os.environ.get("E2E_SEED_ADMIN_PASSWORD", _DEFAULT_PASSWORD)

    # auth schema への write は table owner (vector) role で接続する必要がある。
    # alembic env.py と同じ fallback (migration_database_url or database_url)。
    db_url = settings.migration_database_url or settings.database_url
    engine = create_app_engine(db_url, application_name="vector-cli-seed-e2e-users")
    now = datetime.datetime.now(datetime.UTC)
    try:
        async with engine.begin() as conn:
            # raw SQL placeholder は asyncpg が prepared statement で型推論する。
            # auth schema は id 列 (uuid) と email/role/accountId 列 (text) が
            # 混在するため、文字列値の placeholder を片方が uuid 列・もう片方が
            # text 列にも bind されると AmbiguousParameterError が出る。
            # 全ての placeholder を明示 CAST して型を確定させる。
            await conn.execute(
                text(
                    'INSERT INTO auth."user" '
                    '(id, name, email, "emailVerified", "createdAt", '
                    '"updatedAt", role) VALUES '
                    "(CAST(:uid AS uuid), 'E2E User', :uemail, true, "
                    ":now, :now, 'user'), "
                    "(CAST(:aid AS uuid), 'E2E Admin', :aemail, true, "
                    ":now, :now, 'admin') "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "uid": _USER_ID,
                    "aid": _ADMIN_ID,
                    "uemail": user_email,
                    "aemail": admin_email,
                    "now": now,
                },
            )
            # accountId (text) と userId (uuid) は同じ user UUID 文字列だが、
            # asyncpg prepared statement では同じ placeholder の input type を
            # 一意決定できない (CAST(:x AS uuid) と :x text 列の同居で
            # AmbiguousParameterError)。placeholder を分離して各 column ごとに
            # 個別 bind することで型を確定させる。
            await conn.execute(
                text(
                    "INSERT INTO auth.account "
                    '(id, "accountId", "providerId", "userId", password, '
                    '"createdAt", "updatedAt") VALUES '
                    "(CAST(:uacc AS uuid), :uid_text, 'credential', "
                    "CAST(:uid_uuid AS uuid), :upass, :now, :now), "
                    "(CAST(:aacc AS uuid), :aid_text, 'credential', "
                    "CAST(:aid_uuid AS uuid), :apass, :now, :now) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "uacc": _USER_ACCOUNT_ID,
                    "aacc": _ADMIN_ACCOUNT_ID,
                    "uid_text": _USER_ID,
                    "uid_uuid": _USER_ID,
                    "aid_text": _ADMIN_ID,
                    "aid_uuid": _ADMIN_ID,
                    "upass": _better_auth_scrypt_hash(user_pw, _USER_SALT_HEX),
                    "apass": _better_auth_scrypt_hash(admin_pw, _ADMIN_SALT_HEX),
                    "now": now,
                },
            )
    finally:
        await engine.dispose()


def main() -> None:
    # backend/CLAUDE.md は app/ 配下での os.environ 直参照を禁止するが、scripts/
    # は CI/dev 専用 entry point で、production guard を settings field 経由に
    # する場合 settings 自体に env field を新設する必要があり scope 拡大になる。
    # ここでは guard 用に直参照する。production では物理的に i4 migration が
    # 存在しないので E2E user は流入しないが、運用者が誤って本 script を打った
    # 場合の最後の防御線として fail-fast する。
    if os.environ.get("ENV", "").lower() == "production":
        print(
            "ERROR: seed_e2e_users.py must NOT run in production "
            "(red-team C2 defense).",
            file=sys.stderr,
        )
        sys.exit(2)
    asyncio.run(_seed())


if __name__ == "__main__":
    main()
