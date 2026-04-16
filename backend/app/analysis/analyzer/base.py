"""API を単発呼び出しする抽象 Analyzer 基底クラス。"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import ClassVar

import structlog

from app.analysis.errors import AnalysisDomainError
from app.models.article_analysis import ImpactLevel

logger = structlog.get_logger(__name__)


@dataclass
class AnalysisData:
    """DB 永続化前のパース済み AI レスポンス。"""

    title: str
    summary: str
    impact_level: ImpactLevel
    reasoning: str
    keywords: list[str] | None = None


class BaseAnalyzer(abc.ABC):
    """AI analyzer のテンプレートメソッド基底。

    サブクラスは以下 3 つのフックを実装する:
    - ``analyze``: プロンプトの構築とレスポンス解析（公開 API）
    - ``_call_api``: SDK の生呼び出し（エラー処理なし）
    - ``_translate_error``: SDK 例外をエラー階層に分類する

    また以下の ClassVar を宣言する必要がある:
    - ``MODEL``: モデル識別子（例: ``"gemini-2.5-flash-lite"``）
    - ``RPM``: 1 分あたりリクエスト上限。無制限なら ``None``
    - ``RPD``: 1 日あたりリクエスト上限。無制限なら ``None``

    レート制限とリトライは Task 層の責務。
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
        """モデル識別子（例: 'gemini-2.5-flash-lite'）。"""
        return self.MODEL

    # ── 抽象フック（サブクラスが実装） ──────────────────────

    @abc.abstractmethod
    async def analyze(
        self,
        title: str,
        description: str | None,
        content: str | None = None,
        keywords_by_category: dict[str, list[str]] | None = None,
    ) -> AnalysisData:
        """記事を分析し、構造化した分析データを返す。

        Args:
            title: 英語記事タイトル。
            description: 英語記事の概要（None の場合あり）。
            content: 記事本文全文（None の場合あり）。
            keywords_by_category: カテゴリ別キーワード候補の辞書（任意）。
                キーはカテゴリ slug、値はキーワード名のリスト。
                AI が全カテゴリを横断して最も関連性の高いものを選ぶ。

        Returns:
            日本語訳・インパクトレベル・根拠を含む AnalysisData。

        Raises:
            AnalysisDomainError: 分析に失敗した場合。
        """
        ...

    @abc.abstractmethod
    async def _call_api(self, prompt: str) -> str:
        """プロバイダー SDK を呼び出し、生のテキストレスポンスを返す。

        例外は捕捉せず ``_call_once`` に伝播させること。
        """
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """SDK 例外をエラー階層に分類する。

        対応する AnalysisDomainError サブクラスを raise ではなく return で返す。
        """
        ...

    # ── 単発呼び出し ──────────────────────────────────

    async def _call_once(self, prompt: str) -> str:
        """プロバイダー API を 1 回呼び出し、例外をエラー階層に変換する。

        リトライとレート制限は Task 層の責務。
        """
        try:
            logger.info("analyzer_api_call", model=self.model_name)
            result = await self._call_api(prompt)
            logger.info("analyzer_api_success", model=self.model_name)
            return result
        except AnalysisDomainError:
            raise
        except Exception as exc:
            raise self._translate_error(exc) from exc
