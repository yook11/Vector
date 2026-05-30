"""内部 JWT decode の構造的検証テスト。

`_decode_internal_jwt` が以下を構造的に reject することを担保する:
- exp 不在 (永続 admin 化攻撃の起点)
- exp 切れ
- iss 不一致 / 不在
- aud 不一致 / 不在
- sub 不在
- role 不在

`AsyncClient` を介さず dependency function を直呼びすることで validation logic
を絞ってテストする。DB を触らないため unit マーカーが付与され、postgres 不要で
走る (CI の backend-unit job で実行可能)。
"""

import time

import jwt
import pytest
from fastapi import HTTPException

from app.config import settings
from app.dependencies import get_current_user, get_optional_user

_SECRET = settings.bff_jwt_signing_secret.get_secret_value()
_ALGO = "HS256"
_USER_ID = "00000000-0000-4000-a000-000000000099"


def _encode(claims: dict[str, object]) -> str:
    return jwt.encode(claims, _SECRET, algorithm=_ALGO)


def _valid_claims() -> dict[str, object]:
    now = int(time.time())
    return {
        "sub": _USER_ID,
        "role": "user",
        "iss": "vector-bff",
        "aud": "vector-backend",
        "iat": now,
        "exp": now + 60,
    }


@pytest.mark.asyncio
class TestInternalJwtDecode:
    async def test_valid_jwt_passes(self) -> None:
        token = _encode(_valid_claims())
        user = await get_current_user(authorization=f"Bearer {token}")
        assert str(user.id) == _USER_ID

    async def test_missing_exp_rejected(self) -> None:
        """exp 不在は 401。"""
        claims = _valid_claims()
        del claims["exp"]
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_expired_jwt_rejected(self) -> None:
        claims = _valid_claims()
        claims["exp"] = int(time.time()) - 1
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_missing_iss_rejected(self) -> None:
        claims = _valid_claims()
        del claims["iss"]
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_wrong_iss_rejected(self) -> None:
        claims = _valid_claims()
        claims["iss"] = "evil-bff"
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_missing_aud_rejected(self) -> None:
        claims = _valid_claims()
        del claims["aud"]
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_wrong_aud_rejected(self) -> None:
        claims = _valid_claims()
        claims["aud"] = "evil-backend"
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_missing_sub_rejected(self) -> None:
        claims = _valid_claims()
        del claims["sub"]
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_missing_role_rejected(self) -> None:
        claims = _valid_claims()
        del claims["role"]
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_get_optional_user_returns_none_for_invalid(self) -> None:
        """get_optional_user は同条件で None を返す (raise しない)。"""
        claims = _valid_claims()
        del claims["exp"]
        token = _encode(claims)
        user = await get_optional_user(authorization=f"Bearer {token}")
        assert user is None
