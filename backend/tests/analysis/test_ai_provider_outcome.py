"""``is_infra_provider_error`` の分類契約 (正本)。

processing_outcome metric が provider 由来失敗を infra_error (分母外) と failed
(分母に算入) に振り分ける述語の真偽表と、**全 provider error leaf が明示分類される**
網羅性を固定する。後者は新 subclass を分類に載せ忘れたら落ちる回帰ガード。
"""

from __future__ import annotations

from enum import StrEnum

import pytest

from app.analysis import ai_provider_errors as taxonomy
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderContentError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderStateError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.ai_provider_outcome import (
    _FAILED_PROVIDER_ERRORS,
    _INFRA_PROVIDER_ERRORS,
    is_infra_provider_error,
)


class _Reason(StrEnum):
    SAMPLE = "sample"


def _instantiate(error_type: type[AIProviderError]) -> AIProviderError:
    """leaf を最小引数で生成する (Content error のみ reason 必須)。"""
    if issubclass(error_type, AIProviderContentError):
        return error_type(reason=_Reason.SAMPLE)
    return error_type()


# 真偽表は spec §2.3 から直書きする (production 述語を呼んで作らない)。
_INFRA_CASES = (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
    AIProviderConfigurationError,
    AIProviderInsufficientBalanceError,
)
_FAILED_CASES = (
    AIProviderRequestInvalidError,
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)


@pytest.mark.parametrize("error_type", _INFRA_CASES)
def test_infra_provider_errors_are_infra(
    error_type: type[AIProviderError],
) -> None:
    """環境・設定・課金・依存先で直る provider error は infra (True)。"""
    assert is_infra_provider_error(_instantiate(error_type)) is True


@pytest.mark.parametrize("error_type", _FAILED_CASES)
def test_failed_provider_errors_are_not_infra(
    error_type: type[AIProviderError],
) -> None:
    """request 構造・対象内容が原因の provider error は failed (False)。"""
    assert is_infra_provider_error(_instantiate(error_type)) is False


def test_none_is_not_infra() -> None:
    """provider_error を持たない marker (None) は failed に倒れる (False)。"""
    assert is_infra_provider_error(None) is False


def _production_provider_error_leaves() -> set[type]:
    """production taxonomy module 定義の具象 provider error (State/Content) を集める。

    ``__subclasses__`` は import 済みの test 専用サブクラス (例: 他テストの
    ``_BadStateError(AIProviderStateError)``) も拾い、収集順序で結果が変わる。分類契約の
    対象は production taxonomy なので module 定義クラスのみに限定し、順序依存を排す。
    """
    bases = (AIProviderStateError, AIProviderContentError)
    return {
        obj
        for obj in vars(taxonomy).values()
        if isinstance(obj, type)
        and issubclass(obj, bases)
        and obj not in bases
        and obj.__module__ == taxonomy.__name__
    }


def test_classification_sets_partition_all_provider_error_leaves() -> None:
    """全 provider error 具象 leaf が infra/failed のどちらかに過不足なく分類される。

    どちらの集合にも載らない新 leaf を追加すると本 test が落ち、silent な failed
    default 化を防ぐ (spec §2.4 / §7.6)。
    """
    leaves = _production_provider_error_leaves()
    classified = set(_INFRA_PROVIDER_ERRORS) | set(_FAILED_PROVIDER_ERRORS)
    assert classified == leaves
    assert set(_INFRA_PROVIDER_ERRORS).isdisjoint(_FAILED_PROVIDER_ERRORS)
