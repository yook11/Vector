"""Stage 5 (document 永続化) 専用の抽象 Embedder 基底クラス。

Search BC (query 一時) は独立 hierarchy (``app/search/embedding/``) を持つ。
本 class は document 埋め込みに専念し、公開 API と内部 hook を単一 document
向けに揃える。

サブクラスは ``EmbeddingCallSpec`` (``spec.py``) を ``SPEC`` class attr として
持ち、``model_name`` / ``dimension`` / ``rate_limit_policy`` の各 property 経由で
公開する。
"""

from __future__ import annotations

import abc

import structlog
from pydantic import ValidationError

from app.analysis.ai_provider_errors import AIProviderError
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.analysis.embedding.errors import (
    EmbeddingError,
    EmbeddingResponseInvalidError,
)
from app.analysis.rate_limit import AIModelRateLimitPolicy

logger = structlog.get_logger(__name__)


class BaseEmbedder(abc.ABC):
    """document 埋め込み専用の embedder テンプレートメソッド基底。

    サブクラスは以下 2 つのフックを実装する:
    - ``_call_api``: SDK の生呼び出し（エラー処理なし）
    - ``_translate_error``: SDK 例外を ``AIProvider*Error`` (Stage 中立の
      Layer 2-A 識別 marker) に翻訳する。マップ未知は ``return exc`` で
      caller の bare re-raise に委譲する規約 (Stage 4 BaseAssessor と同形)

    本 class は Stage 5 (embedding BC) 専用 (``app/analysis/embedding/ai/`` 配下)
    のため、AI 境界として 2 種のエラーを構造保証する:

    - SDK 例外 → ``AIProvider*Error`` 階層 (Layer 2-A、``_translate_error`` で翻訳)。
      Stage 5 marker への詰め替えは Service 層 ACL の責務
    - ``embed_document`` の戻り値が VO 構造制約 (768 dim + 有限性 + サニティ範囲)
      を満たさない場合 → ``EmbeddingResponseInvalidError`` (Layer 2-B) として
      本 class 内で詰め替えて raise (下流での再検証を不要にする)

    入力は ``ReadyForEmbedding`` を受ける。Ready 型が処理に必要な値を保証するため、
    本 class は値 fetch / None チェックを自分で行わず、Ready から直接取り出す。

    サブクラスは ``EmbeddingCallSpec`` を保持し、以下 property で契約を満たす:

    - ``model_name``: モデル識別子（例: ``"gemini-embedding-001"``）
    - ``dimension``: 出力ベクトルの次元数（例: ``768``）
    - ``rate_limit_policy``: provider × model 粒度の rate limit policy
    - ``document_prefix``: 文書埋め込み時の prefix（空ならデフォルト ``""``）

    レート制限とリトライは Task 層の責務。
    """

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """モデル識別子（例: 'gemini-embedding-001'）。"""

    @property
    @abc.abstractmethod
    def dimension(self) -> int:
        """出力ベクトルの次元数（例: 768）。"""

    @property
    @abc.abstractmethod
    def rate_limit_policy(self) -> AIModelRateLimitPolicy:
        """provider × model 粒度の rate limit policy。"""

    @property
    def document_prefix(self) -> str:
        """文書埋め込み時の prefix。デフォルトは空文字 (付与なし)。"""
        return ""

    # -- 公開 API（具象） -------------------------------------------------

    async def embed_document(self, ready: ReadyForEmbedding) -> EmbeddingVector:
        """Ready 型を入力に単一ドキュメントを埋め込み、永続化可能な VO で返す。

        VO 構造制約 (768 dim + 有限性 + サニティ範囲) を満たすことを型レベルで
        保証する。違反は ``EmbeddingResponseInvalidError`` (Layer 2-B) に詰め替え。
        """
        text = ready.text_for_embedding
        prefix = self.document_prefix
        prefixed = f"{prefix}{text}" if prefix else text
        raw = await self._embed_once(prefixed)
        return self._to_vector(raw)

    @staticmethod
    def _to_vector(raw: list[float]) -> EmbeddingVector:
        """SDK の生 ``list[float]`` を ``EmbeddingVector`` VO に詰める。

        VO 構造違反 (次元 / 有限性 / サニティ範囲) は Stage 5 Layer 2-B
        (``EmbeddingResponseInvalidError``) として詰め替えて raise する。
        境界が下流の信頼できる形を保証するため、下流での再検証は不要。
        """
        try:
            return EmbeddingVector(root=tuple(raw))
        except ValidationError as exc:
            # ValidationError は vector 値を含みうるため、公開 message には載せない。
            raise EmbeddingResponseInvalidError() from exc

    # -- 単発呼び出し ----------------------------------------------------

    async def _embed_once(self, text: str) -> list[float]:
        """1 回の API call。SDK 例外を ``AIProvider*Error`` 階層に翻訳して raise。

        例外処理:
        - 既に階層内 (``AIProviderError`` / ``EmbeddingError``) の例外は **素通し**
          (二重翻訳防止)
        - それ以外は ``_translate_error`` 経由で翻訳。同じ exc が返ったら
          ``raise`` (from なし、UNKNOWN として catch-all 経路へ)
        - 翻訳された場合のみ ``raise translated from exc`` で原因連鎖
        """
        try:
            logger.info("embed_api_call", model=self.model_name)
            vector = await self._call_api(text)
            logger.info("embed_api_success", model=self.model_name)
            return vector
        except (AIProviderError, EmbeddingError):
            # 既に階層内の例外は二重翻訳しない。
            raise
        except Exception as exc:
            translated = self._translate_error(exc)
            if translated is exc:
                # マップ未知 → catch-all (UNKNOWN 経路)、from を付けず素通し
                raise
            raise translated from exc

    # -- 抽象フック（サブクラスが実装） ------------------------------

    @abc.abstractmethod
    async def _call_api(self, text: str) -> list[float]:
        """プロバイダー SDK を呼び出し、単一ベクトルを返す。

        例外は捕捉せず ``_embed_once`` に伝播させること。
        """
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> Exception:
        """SDK 例外を ``AIProvider*Error`` (Stage 中立) に翻訳する。

        マップ可能なら対応する ``AIProvider*Error`` 派生 instance を返す。
        マップできなければ **入力 ``exc`` をそのまま返す** (caller である
        ``_embed_once`` が ``if translated is exc: raise`` の bare re-raise guard
        で素通しする規約)。Stage 5 marker への詰め替えは Service 層 ACL の責務
        であり、本メソッドは ``AIProvider*Error`` 段階までで停止する。
        """
        ...
