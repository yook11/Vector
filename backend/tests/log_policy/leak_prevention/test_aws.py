"""AWS のアクセスキーIDを種別付きの表記へ置き換える境界。"""

import pytest

from app.log_policy.leak_prevention import redact_aws_access_key_ids

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "access_key_id",
    [
        pytest.param("AKIAIOSFODNN7EXAMPLE", id="long_term"),
        pytest.param("ASIAIOSFODNN7EXAMPLE", id="temporary"),
    ],
)
def test_access_key_id_is_replaced_with_context_kept(access_key_id: str) -> None:
    """長期・一時アクセスキーIDを種別付きの表記に置き換え、周囲の文を残す。"""
    assert redact_aws_access_key_ids(f"for {access_key_id} in request") == (
        "for [redacted:aws_access_key_id] in request"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("AKIAIOSFODNN7EXAMPL", id="too_short"),
        pytest.param("AKIAIOSFODNN7EXAMPLE0", id="uppercase_digit_suffix"),
        pytest.param("0AKIAIOSFODNN7EXAMPLE", id="uppercase_digit_prefix"),
    ],
)
def test_access_key_rule_preserves_text_outside_its_detection_boundary(
    text: str,
) -> None:
    """長さ・前後境界に一致しない文字列を部分置換しない。"""
    assert redact_aws_access_key_ids(text) == text


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "arn:aws:ssm:ap-northeast-1:123456789012:parameter/vector/db", id="arn"
        ),
        pytest.param(
            "vector-db.cluster-abc.ap-northeast-1.rds.amazonaws.com:5432",
            id="rds_endpoint",
        ),
        pytest.param(
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=900",
            id="algorithm_and_expiry",
        ),
    ],
)
def test_access_key_rule_preserves_noncredential_identifiers(text: str) -> None:
    """ARN・RDS接続先・署名アルゴリズムと有効期間はアクセスキーIDとして扱わない。"""
    assert redact_aws_access_key_ids(text) == text
