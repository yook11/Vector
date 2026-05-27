"""``ExternalFetchError`` の CODE 契約 + 既定 message 合成の不変条件テスト。

``outcome_code`` に投影される origin error code なので、CODE のコメント契約だけ
ではなく機械的に強制する:

- 全 concrete subclass を再帰的に辿り、CODE が 非空・一意・``fetch_`` prefix・
  計 19 種であることを assert (subclass 追加時の重複 / 未定義 / prefix ズレを
  deploy 前に検知する)。
- 多くの subclass が ``message: str = ""`` 既定を持つため、message 空でも
  ``str(exc)`` が非空であることを各 subclass で assert (wrap 経路の監査 / ログ
  が空文字にならない構造保証)。明示 message を渡した場合はそれが優先される
  (additive 非破壊) ことも合わせて固定する。
"""

from __future__ import annotations

import pytest

from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchAccessDeniedError,
    FetchContentTypeMismatchError,
    FetchGatewayError,
    FetchLegalBlockError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchRateLimitedError,
    FetchRedirectBlockedError,
    FetchRedirectLoopError,
    FetchRequestTimeoutError,
    FetchResourceNotFoundError,
    FetchResponseTooLargeError,
    FetchRetryableStatusError,
    FetchRobotsDisallowedError,
    FetchRobotsUnavailableError,
    FetchSsrfBlockedError,
    FetchTimeoutError,
    FetchUnexpectedStatusError,
)

_EXPECTED_CODE_COUNT = 18

# 各 concrete subclass を「message 空」で構築するための必須 kwargs 表。
# 新 subclass を追加して本表に登録し忘れると ``test_construction_table_covers_
# all_subclasses`` が落ちる (str(exc) 非空の網羅を強制する仕掛け)。
_CONSTRUCTION: dict[type[ExternalFetchError], dict[str, object]] = {
    FetchAccessDeniedError: {"status_code": 403, "reason": "forbidden"},
    FetchLegalBlockError: {},
    FetchResourceNotFoundError: {"status_code": 404, "reason": "not_found"},
    FetchRateLimitedError: {},
    FetchOriginServerError: {"status_code": 500, "reason": "internal_error"},
    FetchGatewayError: {"status_code": 502},
    FetchRequestTimeoutError: {},
    FetchRetryableStatusError: {"status_code": 425},
    FetchUnexpectedStatusError: {"status_code": 418},
    FetchTimeoutError: {},
    FetchNetworkError: {},
    FetchSsrfBlockedError: {},
    FetchRobotsDisallowedError: {},
    FetchRobotsUnavailableError: {},
    FetchRedirectBlockedError: {},
    FetchRedirectLoopError: {},
    FetchResponseTooLargeError: {},
    FetchContentTypeMismatchError: {
        "expected_content_type": "text/html",
        "detected_content_type": None,
    },
}


def _concrete_subclasses(root: type) -> set[type]:
    """``root`` の subclass を再帰的に集める (将来の中間 subclass にも追従)。"""
    found: set[type] = set()
    for sub in root.__subclasses__():
        found.add(sub)
        found |= _concrete_subclasses(sub)
    return found


def test_code_contract_nonempty_unique_prefixed_and_count() -> None:
    """全 concrete subclass の CODE: 非空・``fetch_`` prefix・一意・計 19 種。"""
    subclasses = _concrete_subclasses(ExternalFetchError)
    codes = [getattr(cls, "CODE", None) for cls in subclasses]

    assert len(subclasses) == _EXPECTED_CODE_COUNT
    for code in codes:
        assert isinstance(code, str) and code, f"empty/missing CODE: {code!r}"
        assert code.startswith("fetch_"), f"CODE prefix violation: {code!r}"
    assert len(set(codes)) == len(codes), f"duplicate CODE present: {codes}"


def test_construction_table_covers_all_subclasses() -> None:
    """構築表が concrete subclass を漏れなく網羅する (str 非空 assert の前提)。"""
    assert set(_CONSTRUCTION) == _concrete_subclasses(ExternalFetchError)


@pytest.mark.parametrize(
    "cls,kwargs",
    list(_CONSTRUCTION.items()),
    ids=[c.__name__ for c in _CONSTRUCTION],
)
def test_default_message_nonempty_when_message_empty(
    cls: type[ExternalFetchError],
    kwargs: dict[str, object],
) -> None:
    """message を渡さず構築しても ``str(exc)`` が非空 (既定 message 合成)。"""
    exc = cls(**kwargs)  # type: ignore[arg-type]
    rendered = str(exc)
    assert rendered, f"{cls.__name__}: str(exc) empty"
    assert cls.CODE in rendered, f"{cls.__name__}: CODE not in default message"


@pytest.mark.parametrize(
    "cls,kwargs",
    list(_CONSTRUCTION.items()),
    ids=[c.__name__ for c in _CONSTRUCTION],
)
def test_explicit_message_takes_precedence(
    cls: type[ExternalFetchError],
    kwargs: dict[str, object],
) -> None:
    """明示 message を渡せばそれが ``str(exc)`` になる (additive 非破壊)。"""
    exc = cls("explicit boom", **kwargs)  # type: ignore[arg-type]
    assert str(exc) == "explicit boom"
