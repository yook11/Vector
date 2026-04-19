"""API を単発呼び出しする抽象 Extractor 基底クラス。"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import ClassVar

import structlog

from app.analysis.errors import AnalysisDomainError
from app.models.article_entity import EntityType

logger = structlog.get_logger(__name__)


@dataclass
class EntityData:
    """抽出されたエンティティ 1 件。"""

    name: str
    type: EntityType


@dataclass
class ExtractionData:
    """DB 永続化前のパース済み抽出結果。"""

    title_ja: str
    summary_ja: str
    entities: list[EntityData]


class BaseExtractor(abc.ABC):
    """Stage 1 — Content Extraction のテンプレートメソッド基底。

    原文を読み、情報を取り出す。判断はしない。

    サブクラスは以下 3 つのフックを実装する:
    - ``extract``: プロンプト構築とレスポンス解析（公開 API）
    - ``_call_api``: SDK の生呼び出し
    - ``_translate_error``: SDK 例外をエラー階層に分類する

    また以下の ClassVar を宣言する必要がある:
    - ``MODEL``: モデル識別子
    - ``RPM``: 1 分あたりリクエスト上限。無制限なら ``None``
    - ``RPD``: 1 日あたりリクエスト上限。無制限なら ``None``
    """

    MODEL: ClassVar[str]
    RPM: ClassVar[int | None]
    RPD: ClassVar[int | None]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        for attr in ("MODEL", "RPM", "RPD"):
            if attr not in cls.__dict__:
                raise TypeError(f"{cls.__name__} must define ClassVar '{attr}'")

    @property
    def model_name(self) -> str:
        """モデル識別子。"""
        return self.MODEL

    # -- 抽象フック --

    @abc.abstractmethod
    async def extract(
        self,
        title: str,
        content: str,
    ) -> ExtractionData:
        """記事から事実を抽出し、構造化データを返す。

        Article の存在が content の品質を保証する（50 文字以上）。

        Args:
            title: 英語記事タイトル（Article.original_title）。
            content: 記事本文全文（Article.original_content）。

        Returns:
            翻訳タイトル・事実ベース要約・エンティティリストを含む ExtractionData。

        Raises:
            AnalysisDomainError: 抽出に失敗した場合。
        """
        ...

    @abc.abstractmethod
    async def _call_api(self, prompt: str) -> str:
        """プロバイダー SDK を呼び出し、生のテキストレスポンスを返す。"""
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """SDK 例外をエラー階層に分類する。"""
        ...

    # -- 単発呼び出し --

    async def _call_once(self, prompt: str) -> str:
        """プロバイダー API を 1 回呼び出し、例外をエラー階層に変換する。"""
        try:
            logger.info("extractor_api_call", model=self.model_name)
            result = await self._call_api(prompt)
            logger.info("extractor_api_success", model=self.model_name)
            return result
        except AnalysisDomainError:
            raise
        except Exception as exc:
            raise self._translate_error(exc) from exc
