"""API を単発呼び出しする抽象 Extractor 基底クラス。"""

from __future__ import annotations

import abc
from typing import ClassVar

import structlog

from app.analysis.errors import AIProviderError, ExtractionDomainError
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.domain import Noise, Signal

logger = structlog.get_logger(__name__)


class BaseExtractor(abc.ABC):
    """Stage 3 — Content Extraction のテンプレートメソッド基底。

    原文を読み、情報を取り出す。判断はしない。

    サブクラスは以下 3 つのフックを実装する:
    - ``extract``: プロンプト構築とレスポンス解析(公開 API)
    - ``_call_api``: SDK の生呼び出し
    - ``_translate_error``: SDK 例外を Layer 2 例外階層に分類する

    また以下の ClassVar を宣言する必要がある:
    - ``MODEL``: モデル識別子
    - ``PROMPT_VERSION``: プロンプト version 識別子 (失敗 audit の
      ``prompt_version`` を埋めるために必須、成功時は envelope が SSoT)
    - ``RPM``: 1 分あたりリクエスト上限。無制限なら ``None``
    - ``RPD``: 1 日あたりリクエスト上限。無制限なら ``None``
    """

    MODEL: ClassVar[str]
    PROMPT_VERSION: ClassVar[str]
    RPM: ClassVar[int | None]
    RPD: ClassVar[int | None]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        for attr in ("MODEL", "PROMPT_VERSION", "RPM", "RPD"):
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
    ) -> ExtractionCall[Signal] | ExtractionCall[Noise]:
        """記事から事実を抽出し、構造化データを返す。

        Article の存在が content の品質を保証する(50 文字以上)。

        Args:
            title: 英語記事タイトル(Article.original_title)。
            content: 記事本文全文(Article.original_content)。

        Returns:
            ``result`` (``Signal`` | ``Noise``) に加え ``raw_response`` /
            ``raw_relevance`` / ``prompt_version`` / ``model_name`` を含む
            Generic envelope。

        Raises:
            AIProviderError: provider 呼び出し由来の失敗 (Layer 2-A)。
                Layer 1 marker (NonRetryableDropArticle / NonRetryableKeepArticle /
                RetryableError) を多重継承しており Task 層で dispatch される。
            ExtractionDomainError: Stage 3 工程由来の失敗 (Layer 2-B)。
                ``ExtractionResponseInvalidError`` 等。
        """
        ...

    @abc.abstractmethod
    async def _call_api(
        self, prompt: str
    ) -> ExtractionCall[Signal] | ExtractionCall[Noise]:
        """プロバイダー SDK を呼び出し、構造化レスポンスを envelope で返す。"""
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> Exception:
        """SDK 例外を Layer 2-A (``AIProviderError``) または Layer 2-B
        (``ExtractionDomainError``) に分類する。

        SDK 例外がいずれの分類にも当てはまらない場合は **生 Exception を
        そのまま return** する (``return exc``)。``_call_once`` 側が
        ``is exc`` をチェックして bare re-raise する (Task 層 catch-all
        UNKNOWN ラベルに流す)。
        """
        ...

    # -- 単発呼び出し --

    async def _call_once(
        self, prompt: str
    ) -> ExtractionCall[Signal] | ExtractionCall[Noise]:
        """プロバイダー API を 1 回呼び出し、例外を Layer 2 階層に変換する。

        ``_translate_error`` が翻訳不可で生 ``exc`` を返した場合は ``raise
        translated from exc`` を避け、bare re-raise する (``__cause__`` の
        自己参照を避けて stacktrace の正常性を保つ)。
        """
        try:
            logger.info("extractor_api_call", model=self.model_name)
            envelope = await self._call_api(prompt)
            logger.info("extractor_api_success", model=self.model_name)
            return envelope
        except (AIProviderError, ExtractionDomainError):
            # 既に Layer 2 に翻訳済 (_call_api 内で raise された)
            raise
        except Exception as exc:
            translated = self._translate_error(exc)
            if translated is exc:
                raise  # bare re-raise — 自己 chain を避け、Task 層 UNKNOWN へ
            raise translated from exc
