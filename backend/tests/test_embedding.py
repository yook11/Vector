"""Embedding サービスと類似記事 API エンドポイントのテスト。"""

import pytest

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.errors import (
    AnalysisDomainError,
    InvalidInputError,
    ProviderError,
)

# ---------------------------------------------------------------------------
# E. BaseEmbedder._embed_once (StubEmbedder)
# ---------------------------------------------------------------------------


class _InvalidInputSDKError(Exception):
    """プロバイダ SDK のクライアントエラーを模す (AnalysisDomainError ではない)。"""


class StubEmbedder(BaseEmbedder):
    """テスト用サブクラス。_call_api の呼び出しを記録し、任意で例外を送出する。

    _call_api は素の例外を送出する (SDK エラーを模す)。
    _translate_error が embedding エラー階層へマップする。
    """

    MODEL = "stub-model"
    DIMENSION = 3
    RPM = None
    RPD = None

    def __init__(
        self, *, side_effects: list[list[list[float]] | Exception] | None = None
    ) -> None:
        self._side_effects = list(side_effects or [])
        self._calls: list[tuple[str | list[str]]] = []

    async def _call_api(self, contents: str | list[str]) -> list[list[float]]:
        self._calls.append((contents,))
        if self._side_effects:
            effect = self._side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return [[0.1, 0.2, 0.3]]

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        if isinstance(exc, _InvalidInputSDKError):
            return InvalidInputError(str(exc))
        return ProviderError(str(exc))


@pytest.mark.asyncio
async def test_embed_document_returns_first_vector() -> None:
    embedder = StubEmbedder(side_effects=[[[1.0, 2.0, 3.0]]])
    result = await embedder.embed_document("hello")
    assert result == [1.0, 2.0, 3.0]


@pytest.mark.asyncio
async def test_embed_documents_returns_all_vectors() -> None:
    vectors = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    embedder = StubEmbedder(side_effects=[vectors])
    result = await embedder.embed_documents(["a", "b"])
    assert result == vectors


@pytest.mark.asyncio
async def test_embed_once_translates_sdk_error() -> None:
    """SDK 例外は _translate_error で変換される。"""
    embedder = StubEmbedder(side_effects=[RuntimeError("API error")])
    with pytest.raises(ProviderError):
        await embedder.embed_document("text")
    assert len(embedder._calls) == 1


@pytest.mark.asyncio
async def test_invalid_input_error_no_retry() -> None:
    embedder = StubEmbedder(side_effects=[_InvalidInputSDKError("bad input")])
    with pytest.raises(InvalidInputError, match="bad input"):
        await embedder.embed_document("text")
    assert len(embedder._calls) == 1


@pytest.mark.asyncio
async def test_prefix_applied() -> None:
    """プレフィックスなしの StubEmbedder はテキストをそのまま渡す。"""
    embedder = StubEmbedder()
    await embedder.embed_document("doc")
    await embedder.embed_query("query")

    assert embedder._calls[0] == ("doc",)
    assert embedder._calls[1] == ("query",)


class PrefixedStubEmbedder(StubEmbedder):
    """プレフィックス付きの StubEmbedder。"""

    MODEL = "stub-model"
    DIMENSION = 3
    RPM = None
    RPD = None
    DOCUMENT_PREFIX = "P: "
    QUERY_PREFIX = "Q: "


@pytest.mark.asyncio
async def test_prefix_prepended_to_text() -> None:
    """プレフィックスが定義されている場合、テキスト先頭に付与される。"""
    embedder = PrefixedStubEmbedder()
    await embedder.embed_document("doc")
    await embedder.embed_query("query")
    await embedder.embed_documents(["a", "b"])

    assert embedder._calls[0] == ("P: doc",)
    assert embedder._calls[1] == ("Q: query",)
    assert embedder._calls[2] == (["P: a", "P: b"],)


# ---------------------------------------------------------------------------
# F. ClassVar enforcement
# ---------------------------------------------------------------------------


def test_base_embedder_rejects_subclass_without_classvar() -> None:
    """必須 ClassVar を欠く具象サブクラスは TypeError を送出する。"""
    with pytest.raises(TypeError, match="must define ClassVar 'RPD'"):

        class BadEmbedder(BaseEmbedder):
            MODEL = "bad"
            DIMENSION = 3
            RPM = None
            # RPD は意図的に未定義

            async def _call_api(self, contents: str | list[str]) -> list[list[float]]:
                return [[0.0]]

            def _translate_error(self, exc: Exception) -> AnalysisDomainError:
                return AnalysisDomainError(str(exc))
