"""API を単発呼び出しする抽象 Classifier 基底クラス。"""

from __future__ import annotations

import abc
from typing import ClassVar

import structlog

from app.analysis.classifier.schema import AssessmentResponse
from app.analysis.errors import AnalysisDomainError

logger = structlog.get_logger(__name__)


class BaseClassifier(abc.ABC):
    """Stage 4 — Assessment のテンプレートメソッド基底。

    Stage 3 (Extraction) の構造化出力に対して判断を下す。原文は読まない。
    判定結果は InScope | OutOfScope の tagged union（``AssessmentResponse``）。

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
    ) -> AssessmentResponse:
        """Stage 3 (Extraction) の出力を判定し、InScope か OutOfScope のいずれかを返す。

        Args:
            title_ja: 日本語翻訳タイトル。
            summary_ja: 事実ベースの日本語要約。

        Returns:
            ``InScope`` または ``OutOfScope``。呼び出し側は ``isinstance`` / ``match``
            で場合分けする。

        Raises:
            AnalysisDomainError: 判定に失敗した場合。
        """
        ...

    @abc.abstractmethod
    async def _call_api(self, prompt: str) -> AssessmentResponse:
        """プロバイダー SDK を呼び出し、構造化レスポンスを返す。"""
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """SDK 例外をエラー階層に分類する。"""
        ...

    # -- 単発呼び出し --

    async def _call_once(self, prompt: str) -> AssessmentResponse:
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
