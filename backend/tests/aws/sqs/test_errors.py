"""SQSのエラーコードを、何が起きたかを表す理由へ翻訳する規則を検証する(SDKの例外を組み立てるか、Stubberで本物のSDKに起こさせる)。"""

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from app.aws.sqs.errors import SqsServiceError, to_message_send_error


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
