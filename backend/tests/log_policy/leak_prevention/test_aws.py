"""AWS のアクセスキーIDと署名付きクエリの認証値を、種別付きの表記へ置き換える境界。"""

import pytest

from app.log_policy.leak_prevention import (
    redact_aws_access_key_ids,
    redact_aws_signed_query_credentials,
)

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
    "name",
    [
        pytest.param("X-Amz-Signature", id="signature"),
        pytest.param("X-Amz-Credential", id="credential"),
        pytest.param("X-Amz-Security-Token", id="session_token"),
        pytest.param("x-amz-signature", id="lowercase_name"),
    ],
)
def test_signed_query_value_is_replaced_with_other_parameters_kept(name: str) -> None:
    """署名付きクエリの認証値をエンコード済み部分ごと置き換え、クエリ名と隣接パラメーターを残す。"""
    text = f"?DBUser=app&{name}=synthetic%2Fprivate%2Bvalue&X-Amz-Expires=900"
    assert redact_aws_signed_query_credentials(text) == (
        f"?DBUser=app&{name}=[redacted:aws_signed_query]&X-Amz-Expires=900"
    )


def test_rds_iam_query_keeps_endpoint_and_noncredential_parameters() -> None:
    """RDS IAM認証文字列の3種の認証値を置き換え、接続先と診断用パラメーターを残す。"""
    text = (
        "db.example.invalid:5432/?Action=connect&DBUser=app"
        "&X-Amz-Credential=ASIAIOSFODNN7EXAMPLE%2F20260918%2Fregion%2Frds-db"
        "&X-Amz-Security-Token=synthetic%2Bsession"
        "&X-Amz-Signature=synthetic-signature&X-Amz-Expires=900"
    )
    assert redact_aws_signed_query_credentials(text) == (
        "db.example.invalid:5432/?Action=connect&DBUser=app"
        "&X-Amz-Credential=[redacted:aws_signed_query]"
        "&X-Amz-Security-Token=[redacted:aws_signed_query]"
        "&X-Amz-Signature=[redacted:aws_signed_query]&X-Amz-Expires=900"
    )


@pytest.mark.parametrize("delimiter", [" ", "\n", "'", '"'])
def test_signed_query_rule_preserves_text_after_value_delimiter(delimiter: str) -> None:
    """空白・改行・引用符より後の文をクエリ値に巻き込まない。"""
    text = f"X-Amz-Signature=synthetic{delimiter}failed"
    assert redact_aws_signed_query_credentials(text) == (
        f"X-Amz-Signature=[redacted:aws_signed_query]{delimiter}failed"
    )


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
def test_aws_rules_preserve_noncredential_identifiers(text: str) -> None:
    """ARN・RDS接続先・署名アルゴリズムと有効期間は認証値として扱わない。"""
    assert redact_aws_signed_query_credentials(redact_aws_access_key_ids(text)) == text
