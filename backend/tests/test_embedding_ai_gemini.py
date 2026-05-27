"""GeminiEmbedder のテスト (Stage 5 document 専用)。

API 呼び出しは google-genai client をモックする。エラーマッピングは
``_translate_error`` を直接呼び出して構造的に検証する。

Stage 5 BC 分離後、``GeminiEmbedder`` は ``ReadyForEmbedding`` を受ける document
専用 hierarchy となった (``embed_query`` / ``embed_documents`` は Search BC 側の
``GeminiQueryEmbedder`` に独立、対応テストは ``tests/search/embedding/`` 配下)。

Stage 5 のエラー taxonomy 整備に追従して Stage 4 ``GeminiAssessor`` と完全同形の
``AIProvider*Error`` 階層 (Layer 2-A、Stage 中立) への翻訳を検証する。
Stage 5 marker (``Embedding*Error``) への詰め替えは Service 層 ACL の責務であり、
本テストの守備範囲外。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import errors as genai_errors

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
)
from app.analysis.embedding.ai.gemini import GeminiEmbedder
from app.analysis.embedding.ai.spec import GEMINI_EMBEDDING_SPEC
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import EmbeddingVector


def _make_embedder() -> GeminiEmbedder:
    """genai.Client を mock した GeminiEmbedder を返す。"""
    with (
        patch("app.analysis.embedding.ai.gemini.genai.Client"),
        patch("app.analysis.embedding.ai.gemini.settings") as mock_settings,
    ):
        mock_settings.gemini_api_key.get_secret_value.return_value = "test-key"
        return GeminiEmbedder()


def _make_embed_response(vectors: list[list[float]]) -> MagicMock:
    """EmbedContentResponse 互換のモックを返す。"""
    embeddings = [MagicMock(values=v) for v in vectors]
    response = MagicMock()
    response.embeddings = embeddings
    return response


def _ready(text: str = "hello") -> ReadyForEmbedding:
    return ReadyForEmbedding(analysis_id=1, text_for_embedding=text, article_id=1)


# ---------------------------------------------------------------------------
# A. Initialization
# ---------------------------------------------------------------------------


def test_init_raises_configuration_error_when_api_key_missing() -> None:
    """API key が空文字なら ``AIProviderConfigurationError`` で初期化失敗。"""
    with patch("app.analysis.embedding.ai.gemini.settings") as mock_settings:
        mock_settings.gemini_api_key.get_secret_value.return_value = ""
        with pytest.raises(AIProviderConfigurationError):
            GeminiEmbedder()


def test_spec_is_gemini_embedding_spec_singleton() -> None:
    """``GeminiEmbedder.SPEC`` は ``GEMINI_EMBEDDING_SPEC`` を参照する。

    spec の値そのものの golden は ``test_embedding_specs.py`` に集約する
    (二重定義回避)。本テストでは class attr が singleton を握っていることのみ
    pin する。
    """
    assert GeminiEmbedder.SPEC is GEMINI_EMBEDDING_SPEC


def test_property_contracts_return_spec_values() -> None:
    """instance の property が ``SPEC`` の値を返す (BaseEmbedder 契約の充足)。"""
    embedder = _make_embedder()
    assert embedder.model_name == GEMINI_EMBEDDING_SPEC.model
    assert embedder.dimension == GEMINI_EMBEDDING_SPEC.dimension
    assert embedder.rate_limit_policy == GEMINI_EMBEDDING_SPEC.rate_limit_policy
    assert embedder.document_prefix == GEMINI_EMBEDDING_SPEC.document_prefix


# ---------------------------------------------------------------------------
# B. embed_document — RETRIEVAL_DOCUMENT 固定経路
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_document_uses_retrieval_document_task_type() -> None:
    embedder = _make_embedder()
    mock_call = AsyncMock(return_value=_make_embed_response([[0.1] * 768]))
    embedder._client.aio.models.embed_content = mock_call

    result = await embedder.embed_document(_ready("hello"))

    assert isinstance(result, EmbeddingVector)
    assert result.to_list() == [0.1] * 768
    assert mock_call.call_count == 1
    config = mock_call.call_args.kwargs["config"]
    assert config.task_type == GEMINI_EMBEDDING_SPEC.task_type
    assert config.output_dimensionality == GEMINI_EMBEDDING_SPEC.output_dimensionality
    assert mock_call.call_args.kwargs["model"] == GEMINI_EMBEDDING_SPEC.model
    assert mock_call.call_args.kwargs["contents"] == "hello"


# ---------------------------------------------------------------------------
# C. レスポンス検証 (response shape 違反は AIProviderRequestInvalidError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_document_raises_request_invalid_when_embeddings_empty() -> None:
    embedder = _make_embedder()
    response = MagicMock()
    response.embeddings = []
    embedder._client.aio.models.embed_content = AsyncMock(return_value=response)

    with pytest.raises(AIProviderRequestInvalidError):
        await embedder.embed_document(_ready())


@pytest.mark.asyncio
async def test_embed_document_raises_request_invalid_when_values_missing() -> None:
    embedder = _make_embedder()
    response = MagicMock()
    response.embeddings = [MagicMock(values=None)]
    embedder._client.aio.models.embed_content = AsyncMock(return_value=response)

    with pytest.raises(AIProviderRequestInvalidError):
        await embedder.embed_document(_ready())


# ---------------------------------------------------------------------------
# D. _translate_error は共通 translator に delegate (smoke のみ)
#
# 分類の網羅は tests/analysis/test_gemini_error_translator.py に集約。
# ここでは delegation が経路として効いていることを最小ケースで確認する。
# ---------------------------------------------------------------------------


def _api_error(
    code: int, status: str, message: str = "msg"
) -> genai_errors.ClientError:
    """``ClientError(code, response_json)`` を簡易構築する helper。"""
    response_json = {"error": {"status": status, "message": message}}
    return genai_errors.ClientError(code, response_json)


def test_delegates_unauthenticated_to_configuration_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_api_error(401, "UNAUTHENTICATED"))
    assert isinstance(result, AIProviderConfigurationError)


def test_delegates_timeout_to_network_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(TimeoutError("deadline"))
    assert isinstance(result, AIProviderNetworkError)


def test_delegates_unknown_returns_exc_for_bare_reraise() -> None:
    embedder = _make_embedder()
    runtime_err = RuntimeError("unexpected")
    result = embedder._translate_error(runtime_err)
    assert result is runtime_err


# ---------------------------------------------------------------------------
# E. SDK 例外伝播経路 (embed_document → _translate_error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_document_translates_rate_limited_error() -> None:
    embedder = _make_embedder()
    embedder._client.aio.models.embed_content = AsyncMock(
        side_effect=_api_error(429, "RESOURCE_EXHAUSTED")
    )

    with pytest.raises(AIProviderRateLimitedError):
        await embedder.embed_document(_ready())
