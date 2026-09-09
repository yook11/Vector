"""送信結果自身の成立条件と、送信対象との照合の境界を検証する。"""

from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from app.outbox.publishing.errors import PublishCleanupError, PublishError
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    PublishFailed,
    PublishSucceeded,
)


@pytest.mark.parametrize("event_id", [None, 1, True, "private-event-id"])
@pytest.mark.parametrize("failed", [False, True])
def test_individual_result_rejects_non_uuid(event_id, failed):
    """成功・失敗ともにIDの暗黙変換をせず、不正値を文面へ出さない。"""
    with pytest.raises(TypeError, match="event_id must be a UUID") as caught:
        if failed:
            PublishFailed(event_id, PublishError())
        else:
            PublishSucceeded(event_id)
    assert str(event_id) not in str(caught.value)


@pytest.mark.parametrize(
    "error",
    [
        None,
        RuntimeError("private-error"),
        PublishCleanupError(original_exception=RuntimeError()),
    ],
)
def test_failed_result_rejects_non_publish_error(error):
    """終了処理の失敗や通常例外をイベントの送信失敗として構築できない。"""
    with pytest.raises(TypeError, match="must contain PublishError") as caught:
        PublishFailed(UUID(int=1), error)
    assert "private-error" not in str(caught.value)


@pytest.mark.parametrize("results", [None, [], [PublishSucceeded(UUID(int=1))]])
def test_batch_rejects_non_tuple(results):
    """可変コンテナをtupleへ変換せず、構築時に拒否する。"""
    with pytest.raises(TypeError, match="results must be a tuple"):
        BatchPublishResult(results)


@pytest.mark.parametrize("entry", [None, "private-body", PublishError()])
def test_batch_rejects_invalid_entry_after_valid_result(entry):
    """後半の不正要素も含めて、結果全体の構築を拒否する。"""
    with pytest.raises(TypeError, match="invalid publisher result type") as caught:
        BatchPublishResult((PublishSucceeded(UUID(int=1)), entry))
    assert "private-body" not in str(caught.value)


def test_results_preserve_values():
    """送信結果の参照を維持し、結果のフィールドは変更できない。"""
    error = PublishError()
    success = PublishSucceeded(UUID(int=1))
    failure = PublishFailed(UUID(int=2), error)
    results = (success, failure)
    batch = BatchPublishResult(results)
    assert batch.results is results
    assert failure.error is error
    for value, field in ((success, "event_id"), (failure, "error"), (batch, "results")):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, None)


def test_batch_leaves_correspondence_to_caller():
    """空や重複を含む結果の依頼との対応は、構築時には判断しない。"""
    assert BatchPublishResult(()).results == ()
    result = PublishSucceeded(UUID(int=1))
    assert BatchPublishResult((result, result)).results == (result, result)
