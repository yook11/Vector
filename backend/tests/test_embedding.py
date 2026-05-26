"""BaseEmbedder の ``_embed_once`` / ``_translate_error`` /
``embed_document`` (VO 詰め替え境界) 振る舞いテスト。

Stage 5 BC 分離後、``BaseEmbedder`` は ``ReadyForEmbedding`` を受ける document
専用 hierarchy となった (``embed_query`` / ``embed_documents`` は Search BC 側の
``app/search/embedding/`` に独立)。本テストは新 interface (``embed_document(ready)``
+ ``_call_api(text: str) -> list[float]``) を前提に書く。

Stage 5 marker (``Embedding*Error``) のうち Layer 2-A 由来 (``AIProviderError`` →
Layer 1 marker) の詰め替えは Service 層 ACL の責務で、本テストは扱わない。
ただし Layer 2-B (``EmbeddingResponseInvalidError``) は embedder 境界内で
詰め替える契約のため、本テストで検証する。
"""

import math

import pytest

from app.analysis.ai_provider_errors import (
    AIProviderInputRejectedError,
    AIProviderRequestInvalidError,
)
from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.analysis.embedding.errors import EmbeddingResponseInvalidError
from app.analysis.rate_limit import RatePolicy

_STUB_RATE_POLICY = RatePolicy(provider="stub", model="stub-model", rpm=None, rpd=None)


def _v(value: float = 0.1) -> list[float]:
    """テスト用の有効な 768 次元ベクトルを生成する。"""
    return [value] * EMBEDDING_DIMENSION


def _ready(text: str = "hello") -> ReadyForEmbedding:
    """テスト用 ReadyForEmbedding を生成する。"""
    return ReadyForEmbedding(analysis_id=1, text_for_embedding=text, article_id=1)


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

    @property
    def model_name(self) -> str:
        return "stub-model"

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIMENSION

    @property
    def rate_policy(self) -> RatePolicy:
        return _STUB_RATE_POLICY

    def __init__(
        self, *, side_effects: list[list[float] | Exception] | None = None
    ) -> None:
        self._side_effects = list(side_effects or [])
        self._calls: list[str] = []

    async def _call_api(self, text: str) -> list[float]:
        self._calls.append(text)
        if self._side_effects:
            effect = self._side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return _v()

    def _translate_error(self, exc: Exception) -> Exception:
        if isinstance(exc, _InvalidInputSDKError):
            # Phase 4: AIProvider*Error は class 識別のみ (SAFE_ATTRS=CODE)。
            return AIProviderInputRejectedError()
        # マップできない例外は exc をそのまま return (bare re-raise 規約)
        return exc


@pytest.mark.asyncio
async def test_embed_document_returns_validated_embedding_vector() -> None:
    """``embed_document`` は永続化可能性を保証する ``EmbeddingVector`` を返す。"""
    raw = _v(0.5)
    embedder = StubEmbedder(side_effects=[raw])
    result = await embedder.embed_document(_ready())
    assert isinstance(result, EmbeddingVector)
    assert result.to_list() == raw


@pytest.mark.asyncio
async def test_embed_document_wraps_vo_violation_in_layer_2b_marker() -> None:
    """SDK 戻り値が VO 構造制約を破ると embedder 境界が
    ``EmbeddingResponseInvalidError`` (Layer 2-B) に詰め替えて raise する。
    """
    invalid = _v(0.1)
    invalid[0] = math.nan
    embedder = StubEmbedder(side_effects=[invalid])
    with pytest.raises(EmbeddingResponseInvalidError) as exc_info:
        await embedder.embed_document(_ready())
    assert exc_info.value.code == "embedding_response_invalid"
    assert exc_info.value.provider_error is None
    # __cause__ に Pydantic ValidationError が紐付く (audit chain forensics)
    assert exc_info.value.__cause__ is not None


@pytest.mark.asyncio
async def test_embed_document_wraps_wrong_dimension_in_layer_2b_marker() -> None:
    """768 次元 ≠ の戻り値も Layer 2-B に詰め替えられる。"""
    embedder = StubEmbedder(side_effects=[[0.1] * (EMBEDDING_DIMENSION - 1)])
    with pytest.raises(EmbeddingResponseInvalidError):
        await embedder.embed_document(_ready())


@pytest.mark.asyncio
async def test_embed_once_translates_sdk_error() -> None:
    """SDK 例外は _translate_error で AIProvider*Error にマップされる。

    Phase 4: AIProvider*Error は VectorDomainError 継承で __str__ 経路に PII を
    乗せない。class 名で発火経路を pin する (旧 match=message は str(exc) に
    出ない)。
    """
    embedder = StubEmbedder(side_effects=[_InvalidInputSDKError("bad input")])
    with pytest.raises(AIProviderInputRejectedError):
        await embedder.embed_document(_ready())
    assert len(embedder._calls) == 1


@pytest.mark.asyncio
async def test_embed_once_passes_through_unmapped_exception() -> None:
    """マップ未知の例外は bare re-raise (``translated is exc`` 経路) で素通し。"""
    sentinel = RuntimeError("unmapped failure mode")
    embedder = StubEmbedder(side_effects=[sentinel])
    with pytest.raises(RuntimeError) as exc_info:
        await embedder.embed_document(_ready())
    # bare re-raise なので __cause__ は付かない (translated is exc 経路)
    assert exc_info.value is sentinel
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_embed_once_does_not_double_translate_ai_provider_error() -> None:
    """``AIProviderError`` 階層は ``_translate_error`` を経由せず素通し。"""
    pre_translated = AIProviderRequestInvalidError("already translated")
    embedder = StubEmbedder(side_effects=[pre_translated])
    with pytest.raises(AIProviderRequestInvalidError) as exc_info:
        await embedder.embed_document(_ready())
    assert exc_info.value is pre_translated


@pytest.mark.asyncio
async def test_no_prefix_passes_text_verbatim() -> None:
    """プレフィックスなしの StubEmbedder はテキストをそのまま渡す。"""
    embedder = StubEmbedder()
    await embedder.embed_document(_ready("doc"))
    assert embedder._calls == ["doc"]


class PrefixedStubEmbedder(StubEmbedder):
    """プレフィックス付きの StubEmbedder (``document_prefix`` を override)。"""

    @property
    def document_prefix(self) -> str:
        return "P: "


@pytest.mark.asyncio
async def test_document_prefix_prepended_to_text() -> None:
    """``document_prefix`` を override した場合、テキスト先頭に付与される。"""
    embedder = PrefixedStubEmbedder()
    await embedder.embed_document(_ready("doc"))
    assert embedder._calls == ["P: doc"]


# ---------------------------------------------------------------------------
# abstract property enforcement
# ---------------------------------------------------------------------------


def test_base_embedder_rejects_subclass_without_required_properties() -> None:
    """必須 abstract property を欠く具象サブクラスは instance 化で TypeError。"""

    class BadEmbedder(BaseEmbedder):
        # model_name / dimension / rate_policy を意図的に未実装

        async def _call_api(self, text: str) -> list[float]:
            return [0.0]

        def _translate_error(self, exc: Exception) -> Exception:
            return exc

    with pytest.raises(TypeError, match="abstract"):
        BadEmbedder()  # type: ignore[abstract]
