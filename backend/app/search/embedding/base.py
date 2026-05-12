"""Search BC 専用の抽象 Query Embedder 基底クラス。

Stage 5 (document 永続化) と独立した hierarchy として再実装する。共用しない
理由 (memory `feedback_no_share_different_problems`):
- Search の query は VO 化せず raw ``list[float]`` で Redis cache に流す
- Stage 5 の document は永続化のため ``EmbeddingVector`` VO に詰め替える
- 一方を変更すると他方に影響する暗黙の結合を作りたくない

``BaseEmbedder`` と同形のテンプレートメソッド (``_call_api`` /
``_translate_error`` の 2 フック) を持つ ABC として再実装する。
"""

from __future__ import annotations

import abc
from typing import ClassVar

import structlog

from app.analysis.errors.provider import AIProviderError

logger = structlog.get_logger(__name__)


class QueryEmbedder(abc.ABC):
    """検索 query 用 embedder のテンプレートメソッド基底。

    サブクラスは以下 2 つのフックを実装する:
    - ``_call_api``: SDK の生呼び出し（エラー処理なし）
    - ``_translate_error``: SDK 例外を ``AIProvider*Error`` (Stage 中立の
      Layer 2-A 識別 marker) に翻訳する。マップ未知は ``return exc`` で
      caller の bare re-raise に委譲する規約

    AI 境界は SDK 例外 → ``AIProvider*Error`` 階層 (Layer 2-A、``_translate_error``
    で翻訳) を構造保証する。HTTP semantics への振り分け (503 vs 422) は
    ``app/search/service.py`` の ACL で行う。

    以下の ClassVar を宣言する必要がある:
    - ``MODEL``: モデル識別子（例: ``"gemini-embedding-001"``）
    - ``DIMENSION``: 出力ベクトルの次元数（例: ``768``）
    - ``QUERY_PREFIX``: 検索クエリ埋め込み時のプレフィックス（空なら付与しない）

    Search BC は per-user 1 日 quota を router で先に消費するため、AI 側の
    rate limit (RPM / RPD) は本 hierarchy では持たない (Stage 5 と異なる)。
    """

    MODEL: ClassVar[str]
    DIMENSION: ClassVar[int]
    QUERY_PREFIX: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        for attr in ("MODEL", "DIMENSION"):
            if attr not in cls.__dict__:
                raise TypeError(f"{cls.__name__} must define ClassVar '{attr}'")

    @property
    def dimension(self) -> int:
        return self.DIMENSION

    @property
    def model_name(self) -> str:
        return self.MODEL

    # -- 公開 API（具象） -------------------------------------------------

    async def embed_query(self, text: str) -> list[float]:
        """検索クエリを埋め込み、raw ``list[float]`` で返す。

        VO 化しない (Search BC の query は Redis cache 用一時値であり、
        永続化を伴わないため)。次元 / 値域の保証は GeminiQueryEmbedder の
        ``_call_api`` 内で provider response shape を検証することで担保する。
        """
        prefixed = f"{self.QUERY_PREFIX}{text}" if self.QUERY_PREFIX else text
        return await self._embed_once(prefixed)

    # -- 単発呼び出し ----------------------------------------------------

    async def _embed_once(self, text: str) -> list[float]:
        """1 回の API call。SDK 例外を ``AIProvider*Error`` 階層に翻訳して raise。

        Pattern (Stage 5 BaseEmbedder._embed_once と同形):
        - 既に階層内 (``AIProviderError``) の例外は **素通し** (二重翻訳防止)
        - それ以外は ``_translate_error`` 経由で翻訳。同じ exc が返ったら
          ``raise`` (from なし、UNKNOWN として catch-all 経路へ)
        - 翻訳された場合のみ ``raise translated from exc`` で原因連鎖
        """
        try:
            logger.info("embed_query_api_call", model=self.model_name)
            vector = await self._call_api(text)
            logger.info("embed_query_api_success", model=self.model_name)
            return vector
        except AIProviderError:
            raise
        except Exception as exc:
            translated = self._translate_error(exc)
            if translated is exc:
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
        で素通しする規約)。HTTP semantics への振り分けは Service 層 ACL の責務。
        """
        ...
