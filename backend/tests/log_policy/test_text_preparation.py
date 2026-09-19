"""内容検出とキー付き値のマスクを、値の準備で合成する順序の契約。"""

from __future__ import annotations

import pytest

from app.log_policy.base import BASE_DENY, BASE_MASK
from app.log_policy.value_preparation import LogValuePreparer

pytestmark = pytest.mark.unit

# 合成値を分割し、秘密検出ツールの規則に一致させない。
_PEM_BLOCK = (
    "-----BEGIN "
    + "PRIVATE KEY-----\nsynthetic-private-body\n-----END PRIVATE KEY-----"
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(
            f"private_key={_PEM_BLOCK} failed", "private_key=*** failed", id="value"
        ),
        pytest.param(
            "private_key=-----BEGIN PRIVATE KEY-----\nsynthetic-private-body",
            "private_key=***",
            id="unterminated_value",
        ),
        pytest.param(
            {f"private_key={_PEM_BLOCK} failed": 1},
            {"private_key=*** failed": 1},
            id="nested_key",
        ),
    ],
)
def test_pem_is_detected_before_masking_assignments(value, expected) -> None:
    """キー付きPEMは値でもキーでもmaskより先に全体を検出し、BEGIN行の途中で置換を止めない。"""
    assert LogValuePreparer(BASE_DENY, mask=BASE_MASK).prepare_field_value(value) == (
        expected
    )


@pytest.mark.parametrize(
    ("text", "mask", "expected"),
    [
        pytest.param(
            "restricted_sample='prefix eyJabc.eyJdef.signature suffix' "
            "got eyJghi.eyJjkl.other failed",
            frozenset({"restricted_sample"}),
            "restricted_sample=*** got eyJ*** failed",
            id="jwt_in_purpose_mask",
        ),
        pytest.param(
            "Authorization: Bearer eyJabc.eyJdef.signature\nupstream failed",
            BASE_MASK,
            "Authorization: ***\nupstream failed",
            id="jwt_in_authorization_header",
        ),
        pytest.param(
            "password='prefix sk-proj-abcdef0123456789ABCDEFxyz suffix' host=db",
            BASE_MASK,
            "password=*** host=db",
            id="provider_key",
        ),
        pytest.param(
            "connection='postgresql://user:synthetic@db/path' retry=1",
            frozenset({"connection"}),
            "connection=*** retry=1",
            id="url_userinfo",
        ),
        pytest.param(
            "request='https://host/?X-Amz-Signature=synthetic&region=x' retry=1",
            frozenset({"request"}),
            "request=*** retry=1",
            id="aws_signed_query",
        ),
    ],
)
def test_mask_hides_entire_value_after_content_detection(text, mask, expected) -> None:
    """内容検出で値の一部を置換した後も、maskは引用符やヘッダー行の値全体を伏せる。"""
    assert LogValuePreparer(BASE_DENY, mask=mask).prepare_field_value(text) == expected


def test_content_detection_runs_without_matching_mask_key() -> None:
    """mask対象キーがなくても文字列内のJWTは内容から検出する。"""
    assert LogValuePreparer(BASE_DENY, mask=frozenset()).prepare_field_value(
        "other=eyJabc.eyJdef.signature"
    ) == ("other=eyJ***")
