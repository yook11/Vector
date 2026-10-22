"""SDKの例外を、何が起きたかを表す送信の失敗へ変換する規則を検証する(SDKの例外を組み立てるか、Stubber・応答の差し替え・資格情報providerで本物のSDKに起こさせる)。"""

import json
from unittest.mock import Mock

import botocore.session
import pytest
from botocore.awsrequest import AWSResponse
from botocore.credentials import ContainerProvider, CredentialResolver
from botocore.exceptions import (
    ClientError,
    CredentialRetrievalError,
    EndpointConnectionError,
    NoCredentialsError,
    NoRegionError,
    ParamValidationError,
    PartialCredentialsError,
)
from botocore.stub import Stubber
from botocore.utils import ContainerMetadataFetcher

from app.aws.sqs.errors import (
    MessageSendConfigurationError,
    MessageSendCredentialsError,
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

QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/q"
SEND_CALLS = [
    ("send_message", {"MessageBody": "{}"}),
    ("send_message_batch", {"Entries": [{"Id": "event", "MessageBody": "{}"}]}),
]


def _client_error(code, status):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "private-sdk-message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "SendMessage",
    )


@pytest.fixture
def sqs_client():
    client = botocore.session.get_session().create_client(
        "sqs",
        region_name="ap-northeast-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    yield client
    client.close()


class _ResponseBody:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, **kwargs):
        yield self._body


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


@pytest.mark.parametrize(("operation", "params"), SEND_CALLS)
def test_real_sdk_client_error_is_translated_without_sdk_message(
    sqs_client, operation, params
):
    """本物のSDKが投げた1件送信・まとめ送信の失敗も同じ理由へ翻訳し、SDKの自由文は持たない。"""
    with Stubber(sqs_client) as stubber:
        stubber.add_client_error(
            operation,
            service_error_code="QueueDoesNotExist",
            service_message="private-sdk-message",
            http_status_code=400,
        )
        with pytest.raises(ClientError) as caught:
            getattr(sqs_client, operation)(QueueUrl=QUEUE_URL, **params)
    error = to_message_send_error(caught.value)
    assert (type(error), error.reason, error.service_code) == (
        SqsServiceError,
        "destination_not_found",
        "QueueDoesNotExist",
    )
    assert "private-sdk-message" not in str(error)
    assert "private-sdk-message" not in repr(error.details)


@pytest.mark.parametrize(("operation", "params"), SEND_CALLS)
def test_real_sdk_query_compatible_code_is_translated_by_original_code(
    sqs_client, monkeypatch, operation, params
):
    """SQSが互換用の別名のコードを返しても、本物のSDKが残した元のコードで「キューがない」と翻訳する。"""
    response = AWSResponse(
        QUEUE_URL,
        400,
        {
            "x-amzn-query-error": "AWS.SimpleQueueService.NonExistentQueue;Sender",
            "content-type": "application/x-amz-json-1.0",
        },
        _ResponseBody(
            json.dumps(
                {
                    "__type": "com.amazonaws.sqs#QueueDoesNotExist",
                    "message": "private-sdk-message",
                }
            ).encode()
        ),
    )
    monkeypatch.setattr(
        sqs_client._endpoint.http_session, "send", Mock(return_value=response)
    )
    with pytest.raises(ClientError) as caught:
        getattr(sqs_client, operation)(QueueUrl=QUEUE_URL, **params)
    error = to_message_send_error(caught.value)
    assert (type(error), error.reason, error.service_code) == (
        SqsServiceError,
        "destination_not_found",
        "QueueDoesNotExist",
    )


@pytest.mark.parametrize(
    ("error", "status", "reason"),
    [
        ({}, 503, "service_unavailable"),
        ({"Code": ""}, 400, "unknown"),
    ],
)
def test_batch_client_error_without_code_is_judged_by_status(error, status, reason):
    """まとめ送信の失敗の応答にコードがなくてもSQSの拒否として扱い、5xxならSQS側の障害、それ以外は未知の理由にする。"""
    translated = to_message_send_error(
        ClientError(
            {"Error": error, "ResponseMetadata": {"HTTPStatusCode": status}},
            "SendMessageBatch",
        )
    )
    assert (type(translated), translated.reason, translated.service_code) == (
        SqsServiceError,
        reason,
        None,
    )


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


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (NoCredentialsError(), "missing_credentials"),
        (
            PartialCredentialsError(provider="env", cred_var="AWS_SECRET_ACCESS_KEY"),
            "incomplete_credentials",
        ),
        (NoRegionError(), "missing_region"),
    ],
)
def test_missing_configuration_keeps_what_is_missing_as_reason(exc, reason):
    """資格情報やリージョンがないSDK例外は設定の不足になり、何がないかをreasonとdetailsに持つ。"""
    error = to_message_send_error(exc)

    assert (type(error), error.reason, error.details) == (
        MessageSendConfigurationError,
        reason,
        {"reason": reason},
    )


@pytest.mark.parametrize(
    ("exc", "exception_type"),
    [
        (
            CredentialRetrievalError(provider="container-role", error_msg="private"),
            "CredentialRetrievalError",
        ),
        (
            ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "private"}},
                "AssumeRole",
            ),
            "ClientError",
        ),
    ],
)
def test_credential_provider_failure_is_neither_configuration_nor_sqs_failure(
    exc, exception_type
):
    """資格情報の取得先の失敗やSTSの拒否は、設定の不足でもSQSの拒否でもない資格情報の取得の失敗になる。"""
    error = to_message_send_error(exc)

    assert (type(error), error.exception_type, error.details) == (
        MessageSendCredentialsError,
        exception_type,
        {"exception_type": exception_type},
    )
    assert "private" not in str(error)


def test_real_container_credential_endpoint_failure_is_credentials_failure():
    """本物のSDKがコンテナの資格情報の取得先に接続できずにクライアントを作れないとき、資格情報の取得の失敗になる。"""
    session = botocore.session.Session()
    session.register_component(
        "credential_provider",
        CredentialResolver(
            [
                ContainerProvider(
                    environ={
                        "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://localhost:1/credentials"
                    },
                    fetcher=ContainerMetadataFetcher(sleep=lambda _: None),
                )
            ]
        ),
    )
    with pytest.raises(CredentialRetrievalError) as caught:
        session.create_client("sqs", region_name="ap-northeast-1")

    error = to_message_send_error(caught.value)

    assert (type(error), error.exception_type) == (
        MessageSendCredentialsError,
        "CredentialRetrievalError",
    )


def test_real_sts_rejection_is_credentials_failure():
    """本物のSDKでSTSがロールの引き受けを拒否したとき、SQSの権限不足ではなく資格情報の取得の失敗になる。"""
    sts = botocore.session.get_session().create_client(
        "sts",
        region_name="ap-northeast-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    try:
        with Stubber(sts) as stubber:
            stubber.add_client_error(
                "assume_role",
                service_error_code="AccessDenied",
                service_message="private-sdk-message",
                http_status_code=403,
            )
            with pytest.raises(ClientError) as caught:
                sts.assume_role(
                    RoleArn="arn:aws:iam::123456789012:role/relay",
                    RoleSessionName="relay",
                )
    finally:
        sts.close()

    error = to_message_send_error(caught.value)

    assert (type(error), error.exception_type) == (
        MessageSendCredentialsError,
        "ClientError",
    )


def test_unclassified_sdk_exception_keeps_exception_type_as_attribute():
    """どの分類にも当たらないSDK例外は想定外の失敗になり、例外の型名を属性とdetailsに持ち、文面にSDKの自由文を出さない。"""
    error = to_message_send_error(ParamValidationError(report="private-report"))

    assert type(error) is MessageSendUnexpectedError
    assert (error.exception_type, error.details, str(error)) == (
        "ParamValidationError",
        {"exception_type": "ParamValidationError"},
        "想定外の理由でメッセージを送信できませんでした",
    )
