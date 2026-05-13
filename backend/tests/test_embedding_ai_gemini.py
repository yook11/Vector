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
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)
from app.analysis.embedding.ai.gemini import GeminiEmbedder
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
        with pytest.raises(AIProviderConfigurationError, match="GEMINI_API_KEY"):
            GeminiEmbedder()


def test_classvars_are_set() -> None:
    """ClassVar の MODEL / DIMENSION が公開仕様どおり。"""
    assert GeminiEmbedder.MODEL == "gemini-embedding-001"
    assert GeminiEmbedder.DIMENSION == 768
    assert GeminiEmbedder.RPM is None
    assert GeminiEmbedder.RPD is None
    assert GeminiEmbedder.DOCUMENT_PREFIX == ""


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
    assert config.task_type == "RETRIEVAL_DOCUMENT"
    assert config.output_dimensionality == 768
    assert mock_call.call_args.kwargs["model"] == "gemini-embedding-001"
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

    with pytest.raises(AIProviderRequestInvalidError, match="no embeddings"):
        await embedder.embed_document(_ready())


@pytest.mark.asyncio
async def test_embed_document_raises_request_invalid_when_values_missing() -> None:
    embedder = _make_embedder()
    response = MagicMock()
    response.embeddings = [MagicMock(values=None)]
    embedder._client.aio.models.embed_content = AsyncMock(return_value=response)

    with pytest.raises(AIProviderRequestInvalidError, match="without values"):
        await embedder.embed_document(_ready())


# ---------------------------------------------------------------------------
# D. _translate_error の分類 (Stage 4 と 1:1 同形)
# ---------------------------------------------------------------------------


def _api_error(
    code: int, status: str, message: str = "msg"
) -> genai_errors.ClientError:
    """``ClientError(code, response_json)`` を簡易構築する helper。

    Stage 4 ``test_assessor_gemini_translate_error.py`` と同形 — nested
    ``error`` キーに status / message を入れて SDK 互換とする。
    """
    response_json = {"error": {"status": status, "message": message}}
    return genai_errors.ClientError(code, response_json)


def _server_error(
    code: int = 500, status: str = "INTERNAL", message: str = "msg"
) -> genai_errors.ServerError:
    response_json = {"error": {"status": status, "message": message}}
    return genai_errors.ServerError(code, response_json)


def test_translate_unauthenticated_to_configuration_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_api_error(401, "UNAUTHENTICATED"))
    assert isinstance(result, AIProviderConfigurationError)


def test_translate_permission_denied_to_configuration_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_api_error(403, "PERMISSION_DENIED"))
    assert isinstance(result, AIProviderConfigurationError)


def test_translate_leaked_key_to_configuration_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(
        _api_error(400, "INVALID_ARGUMENT", "API key reported as leaked")
    )
    assert isinstance(result, AIProviderConfigurationError)


def test_translate_leaked_key_message_is_fixed_string_not_sdk_echo() -> None:
    """red-team chain γ-1: SDK の生 message は捨て固定文言のみを保持する。"""
    embedder = _make_embedder()
    sdk_message = (
        "API key AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q has been reported as leaked"
    )
    result = embedder._translate_error(_api_error(400, "INVALID_ARGUMENT", sdk_message))

    assert isinstance(result, AIProviderConfigurationError)
    assert (
        str(result) == "Gemini API key has been reported as leaked; rotate immediately"
    )
    assert "AIza" not in str(result)


def test_translate_invalid_argument_to_request_invalid_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_api_error(400, "INVALID_ARGUMENT"))
    assert isinstance(result, AIProviderRequestInvalidError)


def test_translate_invalid_argument_safety_blocked_to_input_rejected() -> None:
    """``INVALID_ARGUMENT`` + message に "blocked"/"safety" → InputRejected。"""
    embedder = _make_embedder()
    result = embedder._translate_error(
        _api_error(400, "INVALID_ARGUMENT", "blocked by safety filter")
    )
    assert isinstance(result, AIProviderInputRejectedError)


def test_translate_resource_exhausted_to_rate_limited_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_api_error(429, "RESOURCE_EXHAUSTED"))
    assert isinstance(result, AIProviderRateLimitedError)


def test_translate_resource_exhausted_with_quota_to_quota_exhausted() -> None:
    """message に "quota"/"daily" 含む 429 は QuotaExhausted へ。"""
    embedder = _make_embedder()
    result = embedder._translate_error(
        _api_error(429, "RESOURCE_EXHAUSTED", "daily quota exceeded")
    )
    assert isinstance(result, AIProviderQuotaExhaustedError)


def test_translate_server_error_to_service_unavailable() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_server_error(500, "INTERNAL"))
    assert isinstance(result, AIProviderServiceUnavailableError)


def test_translate_unhandled_client_error_status_returns_exc_for_bare_reraise() -> None:
    """マップ未知の ClientError (code / status 共に翻訳テーブル外) は
    ``exc`` をそのまま return する (bare re-raise 規約)。
    """
    embedder = _make_embedder()
    # code=418 / status=TEAPOT は翻訳テーブルに登録されていない経路
    api_err = _api_error(418, "TEAPOT")
    result = embedder._translate_error(api_err)
    # bare re-raise 規約: マップ未知は exc identity を保ったまま return
    assert result is api_err


def test_translate_timeout_to_network_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(TimeoutError("deadline"))
    assert isinstance(result, AIProviderNetworkError)


def test_translate_connection_error_to_network_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(ConnectionError("refused"))
    assert isinstance(result, AIProviderNetworkError)


def test_translate_unknown_returns_exc_for_bare_reraise() -> None:
    """RuntimeError 等の未知例外は exc をそのまま return (bare re-raise 規約)。"""
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
