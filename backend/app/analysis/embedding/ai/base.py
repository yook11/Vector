"""API を単発呼び出しする抽象 Embedder 基底クラス。"""

from __future__ import annotations

import abc
from typing import ClassVar

import structlog
from pydantic import ValidationError

from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.analysis.embedding.errors import (
    EmbeddingError,
    EmbeddingResponseInvalidError,
)
from app.analysis.errors.provider import AIProviderError

logger = structlog.get_logger(__name__)


class BaseEmbedder(abc.ABC):
    """テキスト embedder のテンプレートメソッド基底。

    サブクラスは以下 2 つのフックを実装する:
    - ``_call_api``: SDK の生呼び出し（エラー処理なし）
    - ``_translate_error``: SDK 例外を ``AIProvider*Error`` (Stage 中立の
      Layer 2-A 識別 marker) に翻訳する。マップ未知は ``return exc`` で
      caller の bare re-raise に委譲する規約 (Stage 4 BaseAssessor と同形)

    本 class は Stage 5 (embedding BC) 専用 (``app/analysis/embedding/ai/`` 配下)
    のため、AI 境界として 2 種のエラーを構造保証する (BC 境界原則:
    feedback_bc_boundary_guarantees_downstream):

    - SDK 例外 → ``AIProvider*Error`` 階層 (Layer 2-A、``_translate_error`` で翻訳)。
      Stage 5 marker への詰め替えは Service 層 ACL の責務
    - ``embed_document`` の戻り値が VO 構造制約 (768 dim + 有限性 + サニティ範囲)
      を満たさない場合 → ``EmbeddingResponseInvalidError`` (Layer 2-B) として
      本 class 内で詰め替えて raise (下流での再検証を不要にする)

    また以下の ClassVar を宣言する必要がある:
    - ``MODEL``: モデル識別子（例: ``"cl-nagoya/ruri-v3-310m"``）
    - ``DIMENSION``: 出力ベクトルの次元数（例: ``768``）
    - ``RPM``: 1 分あたりリクエスト上限。無制限なら ``None``
    - ``RPD``: 1 日あたりリクエスト上限。無制限なら ``None``
    - ``DOCUMENT_PREFIX``: 文書埋め込み時のプレフィックス（空なら付与しない）
    - ``QUERY_PREFIX``: 検索クエリ埋め込み時のプレフィックス

    レート制限とリトライは Task 層の責務。
    """

    MODEL: ClassVar[str]
    DIMENSION: ClassVar[int]
    RPM: ClassVar[int | None]
    RPD: ClassVar[int | None]
    DOCUMENT_PREFIX: ClassVar[str] = ""
    QUERY_PREFIX: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        for attr in ("MODEL", "DIMENSION", "RPM", "RPD"):
            if attr not in cls.__dict__:
                raise TypeError(f"{cls.__name__} must define ClassVar '{attr}'")

    @property
    def dimension(self) -> int:
        """出力ベクトルの次元数（例: 768）。"""
        return self.DIMENSION

    @property
    def model_name(self) -> str:
        """モデル識別子（例: 'gemini-embedding-001'）。"""
        return self.MODEL

    # -- 公開 API（具象） -------------------------------------------------

    async def embed_document(self, text: str) -> EmbeddingVector:
        """単一のドキュメントテキストを埋め込み、永続化可能な VO で返す。

        VO 構造制約 (768 dim + 有限性 + サニティ範囲) を満たすことを型レベルで
        保証する。違反は ``EmbeddingResponseInvalidError`` (Layer 2-B) に詰め替え。
        """
        prefixed = f"{self.DOCUMENT_PREFIX}{text}" if self.DOCUMENT_PREFIX else text
        vectors = await self._embed_once(prefixed)
        return self._to_vector(vectors[0])

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """複数のドキュメントテキストを 1 回の API 呼び出しで埋め込む。"""
        if self.DOCUMENT_PREFIX:
            texts = [f"{self.DOCUMENT_PREFIX}{t}" for t in texts]
        return await self._embed_once(texts)

    async def embed_query(self, text: str) -> list[float]:
        """検索クエリを埋め込む (search BC は Redis cache に list で保存するため
        VO 化しない)。"""
        prefixed = f"{self.QUERY_PREFIX}{text}" if self.QUERY_PREFIX else text
        vectors = await self._embed_once(prefixed)
        return vectors[0]

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
            raise EmbeddingResponseInvalidError(
                f"embedder returned vector violating EmbeddingVector invariants: {exc}"
            ) from exc

    # -- 単発呼び出し ----------------------------------------------------

    async def _embed_once(self, contents: str | list[str]) -> list[list[float]]:
        """1 回の API call。SDK 例外を ``AIProvider*Error`` 階層に翻訳して raise。

        Pattern (Stage 4 BaseAssessor._call_once と同形):
        - 既に階層内 (``AIProviderError`` / ``EmbeddingError``) の例外は **素通し**
          (二重翻訳防止)
        - それ以外は ``_translate_error`` 経由で翻訳。同じ exc が返ったら
          ``raise`` (from なし、UNKNOWN として catch-all 経路へ)
        - 翻訳された場合のみ ``raise translated from exc`` で原因連鎖
        """
        try:
            logger.info(
                "embed_api_call",
                model=self.model_name,
                batch_size=len(contents) if isinstance(contents, list) else 1,
            )
            vectors = await self._call_api(contents)
            logger.info(
                "embed_api_success",
                model=self.model_name,
                count=len(vectors),
            )
            return vectors
        except (AIProviderError, EmbeddingError):
            # 既に階層内 — 二重翻訳防止 (Stage 4 BaseAssessor と同形)
            raise
        except Exception as exc:
            translated = self._translate_error(exc)
            if translated is exc:
                # マップ未知 → catch-all (UNKNOWN 経路)、from を付けず素通し
                raise
            raise translated from exc

    # -- 抽象フック（サブクラスが実装） ------------------------------

    @abc.abstractmethod
    async def _call_api(self, contents: str | list[str]) -> list[list[float]]:
        """プロバイダー SDK を呼び出し、ベクトルのリストを返す。

        単一テキストの場合でも ``list[list[float]]`` を返すこと。
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
