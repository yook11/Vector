"""BaseEmbedder の ``_embed_once`` / ``_translate_error`` /
``embed_document`` (VO 詰め替え境界) 振る舞いテスト。

Stage 5 が Stage 4 と同型の Layer 2-A (``AIProvider*Error``) を介した
SDK 例外翻訳に切り替わったため、テストは新階層 (``AIProvider*Error``)
ベースで書く。Stage 5 marker のうち Layer 2-A 由来 (``AIProviderError`` →
Layer 1 marker) の詰め替えは Service 層 ACL の責務で、本テストは扱わない。
ただし Layer 2-B (``EmbeddingResponseInvalidError``) は embedder 境界内で
詰め替える契約のため、本テストで検証する。
"""

import math

import pytest

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.analysis.embedding.errors import EmbeddingResponseInvalidError
from app.analysis.errors.provider import (
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderRequestInvalidError,
)


def _v(value: float = 0.1) -> list[float]:
    """テスト用の有効な 768 次元ベクトルを生成する。"""
    return [value] * EMBEDDING_DIMENSION


# ---------------------------------------------------------------------------
# BaseEmbedder._embed_once (StubEmbedder)
# ---------------------------------------------------------------------------


class _InvalidInputSDKError(Exception):
    """プロバイダ SDK のクライアントエラーを模す (AIProviderError 階層外)。"""


class StubEmbedder(BaseEmbedder):
    """テスト用サブクラス。_call_api の呼び出しを記録し、任意で例外を送出する。

    _call_api は素の例外を送出する (SDK エラーを模す)。
    _translate_error が AIProvider*Error 階層にマップする
    (マップ未知は ``return exc`` で bare re-raise に委譲)。
    """

    MODEL = "stub-model"
    DIMENSION = EMBEDDING_DIMENSION
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
        return [_v()]

    def _translate_error(self, exc: Exception) -> Exception:
        if isinstance(exc, _InvalidInputSDKError):
            return AIProviderInputRejectedError(str(exc))
        # マップできない例外は exc をそのまま return (bare re-raise 規約)
        return exc


@pytest.mark.asyncio
async def test_embed_document_returns_validated_embedding_vector() -> None:
    """``embed_document`` は永続化可能性を保証する ``EmbeddingVector`` を返す。"""
    raw = _v(0.5)
    embedder = StubEmbedder(side_effects=[[raw]])
    result = await embedder.embed_document("hello")
    assert isinstance(result, EmbeddingVector)
    assert result.to_list() == raw


@pytest.mark.asyncio
async def test_embed_document_wraps_vo_violation_in_layer_2b_marker() -> None:
    """SDK 戻り値が VO 構造制約を破ると embedder 境界が
    ``EmbeddingResponseInvalidError`` (Layer 2-B) に詰め替えて raise する。
    """
    invalid = _v(0.1)
    invalid[0] = math.nan
    embedder = StubEmbedder(side_effects=[[invalid]])
    with pytest.raises(EmbeddingResponseInvalidError) as exc_info:
        await embedder.embed_document("hello")
    assert exc_info.value.code == "embedding_response_invalid"
    assert exc_info.value.provider_error is None
    # __cause__ に Pydantic ValidationError が紐付く (audit chain forensics)
    assert exc_info.value.__cause__ is not None


@pytest.mark.asyncio
async def test_embed_document_wraps_wrong_dimension_in_layer_2b_marker() -> None:
    """768 次元 ≠ の戻り値も Layer 2-B に詰め替えられる。"""
    embedder = StubEmbedder(side_effects=[[[0.1] * (EMBEDDING_DIMENSION - 1)]])
    with pytest.raises(EmbeddingResponseInvalidError):
        await embedder.embed_document("hello")


@pytest.mark.asyncio
async def test_embed_documents_returns_raw_lists() -> None:
    """``embed_documents`` (batch) は raw ``list[float]`` のまま返す
    (VO 詰め替えは ``embed_document`` のみの責務)。
    """
    vectors = [_v(0.3), _v(0.7)]
    embedder = StubEmbedder(side_effects=[vectors])
    result = await embedder.embed_documents(["a", "b"])
    assert result == vectors


@pytest.mark.asyncio
async def test_embed_once_translates_sdk_error() -> None:
    """SDK 例外は _translate_error で AIProvider*Error にマップされる。"""
    embedder = StubEmbedder(side_effects=[_InvalidInputSDKError("bad input")])
    with pytest.raises(AIProviderInputRejectedError, match="bad input"):
        await embedder.embed_document("text")
    assert len(embedder._calls) == 1


@pytest.mark.asyncio
async def test_embed_once_passes_through_unmapped_exception() -> None:
    """マップ未知の例外は bare re-raise (``translated is exc`` 経路) で素通し。"""
    sentinel = RuntimeError("unmapped failure mode")
    embedder = StubEmbedder(side_effects=[sentinel])
    with pytest.raises(RuntimeError) as exc_info:
        await embedder.embed_document("text")
    # bare re-raise なので __cause__ は付かない (translated is exc 経路)
    assert exc_info.value is sentinel
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_embed_once_does_not_double_translate_ai_provider_error() -> None:
    """``AIProviderError`` 階層は ``_translate_error`` を経由せず素通し。"""
    pre_translated = AIProviderRequestInvalidError("already translated")
    embedder = StubEmbedder(side_effects=[pre_translated])
    with pytest.raises(AIProviderRequestInvalidError) as exc_info:
        await embedder.embed_document("text")
    assert exc_info.value is pre_translated


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
    DIMENSION = EMBEDDING_DIMENSION
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
# ClassVar enforcement
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

            def _translate_error(self, exc: Exception) -> Exception:
                return AIProviderError(str(exc))
