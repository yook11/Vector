"""内部 JWT decode と3層 dependency の構造的検証テスト。

`get_current_user` (login 認証) が以下を構造的に reject することを担保する:
- exp 不在 (永続 admin 化攻撃の起点) / exp 切れ
- iss 不一致 / 不在、aud 不一致 / 不在
- sub 不在、role 不在

`require_bff_request` (BFF 経由証明) は sub/role を要求せず、iss/aud/exp/iat の
不正だけで 401 になることを担保する。また「BFF 経由証明 ⊄ login 認証」として、
sub/role を持たない user-less トークンが get_current_user では 401 になることを
固定する。

`AsyncClient` を介さず dependency function を直呼びすることで validation logic
を絞ってテストする。DB を触らないため unit マーカーが付与され、postgres 不要で
走る (CI の backend-unit job で実行可能)。
"""

import time

import jwt
import pytest
from fastapi import HTTPException

from app.config import settings
from app.dependencies import get_current_user, require_bff_request

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


def _bff_only_claims() -> dict[str, object]:
    """user-less な BFF 経由証明 claim (sub/role 無し)。"""
    now = int(time.time())
    return {
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

    async def test_user_less_token_rejected_by_get_current_user(self) -> None:
        """sub/role を持たない BFF 経由証明トークンは login 認証では 401。

        「BFF 経由証明 ⊄ login 認証」をコードで固定する。
        """
        token = _encode(_bff_only_claims())
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401


@pytest.mark.asyncio
class TestRequireBffRequest:
    """require_bff_request は BFF 経由証明 (iss/aud/exp/iat) のみ要求する。"""

    async def test_user_less_token_passes(self) -> None:
        """sub/role を持たない user-less トークンが通る (None 返却・例外なし)。"""
        token = _encode(_bff_only_claims())
        assert await require_bff_request(authorization=f"Bearer {token}") is None

    async def test_full_user_token_passes(self) -> None:
        """sub/role 付きの user トークンも BFF 経由証明を満たす。"""
        token = _encode(_valid_claims())
        assert await require_bff_request(authorization=f"Bearer {token}") is None

    async def test_missing_authorization_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await require_bff_request(authorization=None)
        assert exc_info.value.status_code == 401

    async def test_missing_iss_rejected(self) -> None:
        claims = _bff_only_claims()
        del claims["iss"]
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await require_bff_request(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_wrong_iss_rejected(self) -> None:
        claims = _bff_only_claims()
        claims["iss"] = "evil-bff"
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await require_bff_request(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_missing_aud_rejected(self) -> None:
        claims = _bff_only_claims()
        del claims["aud"]
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await require_bff_request(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_wrong_aud_rejected(self) -> None:
        claims = _bff_only_claims()
        claims["aud"] = "evil-backend"
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await require_bff_request(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_missing_exp_rejected(self) -> None:
        claims = _bff_only_claims()
        del claims["exp"]
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await require_bff_request(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_expired_rejected(self) -> None:
        claims = _bff_only_claims()
        claims["exp"] = int(time.time()) - 1
        token = _encode(claims)
        with pytest.raises(HTTPException) as exc_info:
            await require_bff_request(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401

    async def test_bad_signature_rejected(self) -> None:
        # 実 secret を改変した別鍵で署名し署名不正を作る (鍵リテラルは持たない)。
        token = jwt.encode(_bff_only_claims(), _SECRET + "-tampered", algorithm=_ALGO)
        with pytest.raises(HTTPException) as exc_info:
            await require_bff_request(authorization=f"Bearer {token}")
        assert exc_info.value.status_code == 401
