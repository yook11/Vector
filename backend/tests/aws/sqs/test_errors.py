"""SQS送信例外の保持情報と標準文字列表現。"""

from app.aws.sqs.errors import SqsSendError, SqsSendFailure, SqsSendFailureKind


def test_sqs_send_error_directly_inherits_exception():
    """SQS送信例外は通常のExceptionを直接継承する。"""
    assert SqsSendError.__bases__ == (Exception,)


def test_sqs_send_error_preserves_failure():
    """失敗の事実を同じインスタンスで保持する。"""
    failure = SqsSendFailure(
        SqsSendFailureKind.THROTTLED, service_code="RequestThrottled"
    )
    assert SqsSendError(failure).failure is failure


def test_sqs_send_error_has_no_implicit_message():
    """失敗属性を例外メッセージへ補完しない。"""
    error = SqsSendError(SqsSendFailure(SqsSendFailureKind.THROTTLED))
    assert error.args == ()
    assert str(error) == ""
