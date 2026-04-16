"""API を単発呼び出しする抽象 Embedder 基底クラス。"""

from __future__ import annotations

import abc
from typing import ClassVar

import structlog

from app.analysis.errors import AnalysisDomainError

logger = structlog.get_logger(__name__)


class BaseEmbedder(abc.ABC):
    """テキスト embedder のテンプレートメソッド基底。

    サブクラスは以下 2 つのフックを実装する:
    - ``_call_api``: SDK の生呼び出し（エラー処理なし）
    - ``_translate_error``: SDK 例外をエラー階層に分類する

    また以下の ClassVar を宣言する必要がある:
    - ``MODEL``: モデル識別子（例: ``"gemini-embedding-001"``）
    - ``DIMENSION``: 出力ベクトルの次元数（例: ``768``）
    - ``RPM``: 1 分あたりリクエスト上限。無制限なら ``None``
    - ``RPD``: 1 日あたりリクエスト上限。無制限なら ``None``

    レート制限とリトライは Task 層の責務。
    """

    MODEL: ClassVar[str]
    DIMENSION: ClassVar[int]
    RPM: ClassVar[int | None]
    RPD: ClassVar[int | None]

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

    async def embed_document(self, text: str) -> list[float]:
        """単一のドキュメントテキストを埋め込む。"""
        vectors = await self._embed_once(text, "RETRIEVAL_DOCUMENT")
        return vectors[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """複数のドキュメントテキストを 1 回の API 呼び出しで埋め込む。"""
        return await self._embed_once(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        """検索クエリを埋め込む。"""
        vectors = await self._embed_once(text, "RETRIEVAL_QUERY")
        return vectors[0]

    # -- 単発呼び出し ----------------------------------------------------

    async def _embed_once(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        """プロバイダー API を 1 回呼び出し、例外をエラー階層に変換する。

        リトライとレート制限は Task 層の責務。
        """
        try:
            logger.info(
                "embed_api_call",
                model=self.model_name,
                task_type=task_type,
                batch_size=len(contents) if isinstance(contents, list) else 1,
            )
            vectors = await self._call_api(contents, task_type)
            logger.info(
                "embed_api_success",
                model=self.model_name,
                count=len(vectors),
            )
            return vectors
        except AnalysisDomainError:
            raise
        except Exception as exc:
            raise self._translate_error(exc) from exc

    # -- 抽象フック（サブクラスが実装） ------------------------------

    @abc.abstractmethod
    async def _call_api(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        """プロバイダー SDK を呼び出し、ベクトルのリストを返す。

        単一テキストの場合でも ``list[list[float]]`` を返すこと。
        例外は捕捉せず ``_embed_once`` に伝播させること。
        """
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """SDK 例外をエラー階層に分類する。

        対応する AnalysisDomainError サブクラスを raise ではなく return で返す。
        """
        ...
