"""取得依頼の送信の失敗を送り直すかの判断。SQSへ送らず、分類後の例外を直接渡す。"""

from __future__ import annotations

import pytest

from app.aws.sqs.errors import MessageSendTransportError
from app.collection.article_acquisition.acquisition_retry import (
    is_transient_send_failure,
)
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)


@pytest.mark.parametrize(
    ("failure", "exception_type", "expected"),
    [
        (
            HttpTransportFailure(
                HttpTransportStage.UNKNOWN, HttpTransportFailureReason.TLS
            ),
            "SSLError",
            True,
        ),
        (
            HttpTransportFailure(
                HttpTransportStage.UNKNOWN, HttpTransportFailureReason.UNKNOWN
            ),
            "HTTPClientError",
            False,
        ),
    ],
)
def test_transport_failure_is_resent_only_when_retry_may_resolve_it(
    failure: HttpTransportFailure, exception_type: str, expected: bool
) -> None:
    """通信の失敗は、証明書以外のTLSなら送り直し、分類できない失敗なら送り直さない。"""
    error = MessageSendTransportError(failure=failure, exception_type=exception_type)
    assert is_transient_send_failure(error) is expected
