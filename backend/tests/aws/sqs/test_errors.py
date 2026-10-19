"""SQSのエラーコードを、何が起きたかを表す理由へ翻訳する規則を検証する(SDKの例外を組み立てるか、Stubberで本物のSDKに起こさせる)。"""

import botocore.session
import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
)
from botocore.stub import Stubber

from app.aws.sqs.errors import (
    MessageSendConfigurationError,
    MessageSendTransportError,
    MessageSendUnexpectedError,
    SqsServiceError,
    to_message_send_error,
    to_sqs_service_error,
)
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)


def _client_error(code, status):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "private-sdk-message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "SendMessage",
    )


@pytest.mark.parametrize(
    ("code", "status", "reason"),
    [
        ("RequestThrottled", 400, "throttled"),
        ("KmsThrottled", 400, "throttled"),
        ("InternalFailure", 500, "service_unavailable"),
        ("ServiceUnavailable", 503, "service_unavailable"),
        ("InvalidClientTokenId", 403, "authentication_failed"),
        ("AccessDenied", 403, "access_denied"),
        ("KmsAccessDenied", 400, "access_denied"),
        ("QueueDoesNotExist", 400, "destination_not_found"),
        ("InvalidMessageContents", 400, "request_rejected"),
        ("IncompleteSignature", 400, "security_rejected"),
        ("RequestExpired", 400, "request_expired"),
        ("KmsDisabled", 400, "encryption_error"),
    ],
)
def test_known_codes_become_what_happened(code, status, reason):
    """SQSの既知のエラーコードを何が起きたかを表す理由に翻訳し、元のコードも残す。"""
    error = to_message_send_error(_client_error(code, status))
    assert (type(error), error.reason, error.service_code) == (
        SqsServiceError,
        reason,
        code,
    )


@pytest.mark.parametrize(
    ("code", "status", "reason"),
    [
        ("AccessDenied", 503, "access_denied"),
        ("FutureError", 503, "service_unavailable"),
        ("FutureError", 400, "unknown"),
    ],
)
def test_status_is_used_only_for_unknown_codes(code, status, reason):
    """既知のコードはstatusより優先し、未知のコードだけ5xxをSQS側の障害とみなす。"""
    error = to_message_send_error(_client_error(code, status))
    assert (type(error), error.reason, error.service_code) == (
        SqsServiceError,
        reason,
        code,
    )


@pytest.mark.parametrize(
    ("code", "reason", "service_code"),
    [
        ("BatchEntryIdsNotDistinct", "request_rejected", "BatchEntryIdsNotDistinct"),
        ("RequestThrottled", "throttled", "RequestThrottled"),
        ("FutureError", "unknown", "FutureError"),
        ("A" * 128, "unknown", "A" * 128),
        ("A" * 129, "unknown", None),
        ("Access Denied", "unknown", None),
        (None, "unknown", None),
    ],
)
def test_code_without_status_is_translated_by_code_alone(code, reason, service_code):
    """statusのないコード(まとめ送信の項目ごとの拒否)も同じ理由へ翻訳し、形の不正なコードは持たない。"""
    error = to_sqs_service_error(code)
    assert (error.reason, error.service_code) == (reason, service_code)


def test_malformed_code_with_server_status_is_service_unavailable():
    """形の不正なコードは未知のコードとして扱い、5xxならSQS側の障害とみなす。"""
    error = to_sqs_service_error("Access Denied", status_code=503)
    assert (error.reason, error.service_code) == ("service_unavailable", None)


def test_real_sdk_client_error_is_translated_without_sdk_message():
    """本物のSDKが投げたSendMessageの失敗も同じ理由へ翻訳し、SDKの自由文は持たない。"""
    client = botocore.session.get_session().create_client(
        "sqs",
        region_name="ap-northeast-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    try:
        with Stubber(client) as stubber:
            stubber.add_client_error(
                "send_message",
                service_error_code="QueueDoesNotExist",
                service_message="private-sdk-message",
                http_status_code=400,
            )
            with pytest.raises(ClientError) as caught:
                client.send_message(
                    QueueUrl="https://sqs.ap-northeast-1.amazonaws.com/123456789012/q",
                    MessageBody="{}",
                )
    finally:
        client.close()
    error = to_message_send_error(caught.value)
    assert (type(error), error.reason, error.service_code) == (
        SqsServiceError,
        "destination_not_found",
        "QueueDoesNotExist",
    )
    assert "private-sdk-message" not in str(error)
    assert "private-sdk-message" not in repr(error.details)


def test_sdk_connection_failure_keeps_transport_facts_as_attributes():
    """SDKの接続失敗は、通信の段階・理由と例外の型名を属性に持ち、同じ値をdetailsに出す。"""
    error = to_message_send_error(
        EndpointConnectionError(endpoint_url="https://sqs.invalid")
    )

    assert type(error) is MessageSendTransportError
    assert (error.failure, error.exception_type, error.details) == (
        HttpTransportFailure(
            HttpTransportStage.CONNECT, HttpTransportFailureReason.NETWORK_IO
        ),
        "EndpointConnectionError",
        {
            "stage": "connect",
            "reason": "network_io",
            "proxy_status": None,
            "exception_type": "EndpointConnectionError",
        },
    )


def test_missing_credentials_keep_exception_type_as_attribute():
    """資格情報がないSDK例外は設定の不足になり、例外の型名を属性とdetailsの両方に持つ。"""
    error = to_message_send_error(NoCredentialsError())

    assert type(error) is MessageSendConfigurationError
    assert (error.exception_type, error.details) == (
        "NoCredentialsError",
        {"exception_type": "NoCredentialsError"},
    )


def test_configuration_failure_without_sdk_exception_has_no_exception_type():
    """SDK例外を伴わない設定の不足は、例外の型名を持たずdetailsも出さない。"""
    error = MessageSendConfigurationError()

    assert (error.exception_type, error.details) == (None, None)


def test_unclassified_sdk_exception_keeps_exception_type_as_attribute():
    """どの分類にも当たらないSDK例外は想定外の失敗になり、例外の型名を属性とdetailsの両方に持つ。"""
    error = to_message_send_error(ParamValidationError(report="private-report"))

    assert type(error) is MessageSendUnexpectedError
    assert (error.exception_type, error.details) == (
        "ParamValidationError",
        {"exception_type": "ParamValidationError"},
    )
