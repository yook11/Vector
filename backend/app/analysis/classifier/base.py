"""API を単発呼び出しする抽象 Classifier 基底クラス。"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import ClassVar

import structlog

from app.analysis.errors import AnalysisDomainError
from app.analysis.extractor.base import EntityData
from app.models.article_analysis import ImpactLevel

logger = structlog.get_logger(__name__)


@dataclass
class ClassificationData:
    """DB 永続化前のパース済み分類結果。"""

    category_slug: str
    topic_name: str
    impact_level: ImpactLevel
    reasoning: str


class BaseClassifier(abc.ABC):
    """Stage 2 — Classification のテンプレートメソッド基底。

    Stage 1 の構造化出力に対して判断を下す。原文は読まない。

    サブクラスは以下 3 つのフックを実装する:
    - ``classify``: プロンプト構築とレスポンス解析（公開 API）
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
    async def classify(
        self,
        title_ja: str,
        summary_ja: str,
        entities: list[EntityData],
        existing_topics_by_category: dict[str, list[str]] | None = None,
    ) -> ClassificationData:
        """Stage 1 の出力を分類し、カテゴリ・トピック・インパクトを返す。

        Args:
            title_ja: 日本語翻訳タイトル。
            summary_ja: 事実ベースの日本語要約。
            entities: 抽出済みエンティティリスト。
            existing_topics_by_category: カテゴリ別の既存トピック名リスト。

        Returns:
            カテゴリ・トピック・インパクト・根拠を含む ClassificationData。

        Raises:
            AnalysisDomainError: 分類に失敗した場合。
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
            logger.info("classifier_api_call", model=self.model_name)
            result = await self._call_api(prompt)
            logger.info("classifier_api_success", model=self.model_name)
            return result
        except AnalysisDomainError:
            raise
        except Exception as exc:
            raise self._translate_error(exc) from exc
