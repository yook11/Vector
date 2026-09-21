"""取得依頼の送信・投入例外の保持契約。"""

from app.collection.article_acquisition.acquisition_dispatch import (
    AcquisitionDispatchError,
    UnsentAcquisitionRequest,
)
from app.collection.article_acquisition.acquisition_send_error import (
    AcquisitionSendFailure,
    RetryDisposition,
    SourceAcquisitionSendError,
)


def test_send_error_directly_inherits_exception():
    """取得依頼の送信例外は通常のExceptionを直接継承する。"""
    assert SourceAcquisitionSendError.__bases__ == (Exception,)


def test_send_error_preserves_failure():
    """再送判断を含む失敗情報を同じインスタンスで保持する。"""
    failure = AcquisitionSendFailure(RetryDisposition.RETRYABLE, "throttled")
    assert SourceAcquisitionSendError(failure).failure is failure


def test_send_error_has_no_implicit_message():
    """送信失敗の属性を例外文面へ自動補完しない。"""
    error = SourceAcquisitionSendError(
        AcquisitionSendFailure(RetryDisposition.RETRYABLE, "throttled")
    )
    assert error.args == ()
    assert str(error) == ""


def test_dispatch_error_directly_inherits_exception():
    """投入例外は通常のExceptionを直接継承する。"""
    assert AcquisitionDispatchError.__bases__ == (Exception,)


def test_dispatch_error_preserves_unsent_requests():
    """未送信依頼と最終失敗情報を改変せず保持する。"""
    failure = AcquisitionSendFailure(RetryDisposition.RETRYABLE, "throttled")
    unsent = (UnsentAcquisitionRequest("request-1", 3, failure),)
    assert AcquisitionDispatchError(unsent).unsent is unsent


def test_dispatch_error_has_no_implicit_message():
    """未送信依頼の情報を例外文面へ自動補完しない。"""
    failure = AcquisitionSendFailure(RetryDisposition.RETRYABLE, "throttled")
    error = AcquisitionDispatchError(
        (UnsentAcquisitionRequest("request-1", 3, failure),)
    )
    assert error.args == ()
    assert str(error) == ""
