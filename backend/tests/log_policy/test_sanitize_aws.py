"""AWS の対象別置換と、共通のキー付き値保護との境界。"""

import pytest

from app.log_policy.sanitize import (
    sanitize_aws_access_key_ids,
    sanitize_aws_signed_query_credentials,
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
    """長期・一時アクセスキーIDは既存の AKIA*** 表記に置換し、周囲の文を残す。"""
    assert sanitize_aws_access_key_ids(f"for {access_key_id} in request") == (
        "for AKIA*** in request"
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
    """既存の長さ・前後境界に一致しない文字列を部分置換しない。"""
    assert sanitize_aws_access_key_ids(text) == text


def test_signed_query_signature_is_hidden_with_other_parameters_kept() -> None:
    """署名値をエンコード済み部分ごと伏せ、隣接パラメーターを残す。"""
    name = "X-Amz-Signature"
    text = f"?DBUser=app&{name}=synthetic%2Fprivate%2Bvalue&X-Amz-Expires=900"
    assert (
        sanitize_aws_signed_query_credentials(text)
        == f"?DBUser=app&{name}=***&X-Amz-Expires=900"
    )


def test_signed_query_credential_is_hidden_with_other_parameters_kept() -> None:
    """Credential値をエンコード済み部分ごと伏せ、隣接パラメーターを残す。"""
    name = "X-Amz-Credential"
    text = f"?DBUser=app&{name}=synthetic%2Fprivate%2Bvalue&X-Amz-Expires=900"
    assert (
        sanitize_aws_signed_query_credentials(text)
        == f"?DBUser=app&{name}=***&X-Amz-Expires=900"
    )


def test_signed_query_session_token_is_hidden_with_other_parameters_kept() -> None:
    """セッショントークンをエンコード済み部分ごと伏せ、隣接パラメーターを残す。"""
    name = "X-Amz-Security-Token"
    text = f"?DBUser=app&{name}=synthetic%2Fprivate%2Bvalue&X-Amz-Expires=900"
    assert (
        sanitize_aws_signed_query_credentials(text)
        == f"?DBUser=app&{name}=***&X-Amz-Expires=900"
    )


def test_signed_query_lowercase_signature_name_is_recognized() -> None:
    """小文字表記の署名クエリ名でも値を伏せる。"""
    name = "x-amz-signature"
    text = f"?DBUser=app&{name}=synthetic%2Fprivate%2Bvalue&X-Amz-Expires=900"
    assert (
        sanitize_aws_signed_query_credentials(text)
        == f"?DBUser=app&{name}=***&X-Amz-Expires=900"
    )


def test_rds_iam_query_keeps_endpoint_and_noncredential_parameters() -> None:
    """RDS IAM認証文字列の3種の認証値を伏せ、接続先と診断用パラメーターを残す。"""
    text = (
        "db.example.invalid:5432/?Action=connect&DBUser=app"
        "&X-Amz-Credential=ASIAIOSFODNN7EXAMPLE%2F20260918%2Fregion%2Frds-db"
        "&X-Amz-Security-Token=synthetic%2Bsession"
        "&X-Amz-Signature=synthetic-signature&X-Amz-Expires=900"
    )
    assert sanitize_aws_signed_query_credentials(text) == (
        "db.example.invalid:5432/?Action=connect&DBUser=app"
        "&X-Amz-Credential=***&X-Amz-Security-Token=***"
        "&X-Amz-Signature=***&X-Amz-Expires=900"
    )


@pytest.mark.parametrize("delimiter", [" ", "\n", "'", '"'])
def test_signed_query_rule_preserves_text_after_value_delimiter(delimiter: str) -> None:
    """空白・改行・引用符より後の文をクエリ値に巻き込まない。"""
    text = f"X-Amz-Signature=synthetic{delimiter}failed"
    assert sanitize_aws_signed_query_credentials(text) == (
        f"X-Amz-Signature=***{delimiter}failed"
    )


def test_aws_rules_preserve_arn() -> None:
    """AWSの対象別規則はARNを認証値として扱わない。"""
    text = "arn:aws:ssm:ap-northeast-1:123456789012:parameter/vector/db"
    assert (
        sanitize_aws_signed_query_credentials(sanitize_aws_access_key_ids(text)) == text
    )


def test_aws_rules_preserve_rds_endpoint() -> None:
    """AWSの対象別規則はRDS接続先を認証値として扱わない。"""
    text = "vector-db.cluster-abc.ap-northeast-1.rds.amazonaws.com:5432"
    assert (
        sanitize_aws_signed_query_credentials(sanitize_aws_access_key_ids(text)) == text
    )


def test_aws_rules_preserve_algorithm_and_expiry() -> None:
    """署名アルゴリズムと有効期間は認証値以外のクエリとして残す。"""
    text = "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=900"
    assert (
        sanitize_aws_signed_query_credentials(sanitize_aws_access_key_ids(text)) == text
    )
