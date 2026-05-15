"""API を単発呼び出しする抽象 Extractor 基底クラス。"""

from __future__ import annotations

import abc

import structlog

from app.analysis.ai_provider_errors import AIProviderError
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.domain import Noise, Signal
from app.analysis.extraction.errors import ExtractionError
from app.analysis.rate_limit import RatePolicy

logger = structlog.get_logger(__name__)


class BaseExtractor(abc.ABC):
    """Stage 3 — Content Extraction のテンプレートメソッド基底。

    原文を読み、情報を取り出す。判断はしない。

    サブクラスは以下のフックを実装する:
    - ``extract``: プロンプト構築とレスポンス解析(公開 API)
    - ``_call_api``: SDK の生呼び出し
    - ``_translate_error``: SDK 例外を Layer 2 例外階層に分類する

    また以下 3 つの abstract property を備える必要がある (構造保証は abc の
    abstract method 検査で得る、ClassVar 強制は持たない):

    - ``model_name``: モデル識別子
    - ``prompt_version``: プロンプト version 識別子 (失敗 audit の
      ``prompt_version`` を埋めるために必須、成功時は envelope が SSoT)
    - ``rate_policy``: provider/model/rpm/rpd を保持する VO
    """

    # -- 抽象 property (call spec exposure) --

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """モデル識別子。"""
        ...

    @property
    @abc.abstractmethod
    def prompt_version(self) -> str:
        """プロンプト version 識別子 (8 文字 hash)。"""
        ...

    @property
    @abc.abstractmethod
    def rate_policy(self) -> RatePolicy:
        """provider × model × RPM × RPD の rate limit policy。"""
        ...

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
            AIProviderError: provider 呼び出し由来の失敗 (Layer 2-A)。Stage 3
                boundary (``ExtractionService.execute`` /
                ``ReExtractionService._extract_once_mapped``) の ACL
                ``map_provider_to_extraction`` で Stage 3 marker に詰め替えられ、
                Task 層で dispatch される。
            ExtractionError: Stage 3 工程由来の失敗 (Layer 2-B)。
                ``ExtractionResponseInvalidError`` 等は extractor 内部で raise
                され、既に Stage 3 Layer 1 marker subclass として伝搬する。
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
        (``ExtractionError``) に分類する。

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
        except (AIProviderError, ExtractionError):
            # 既に Layer 2 に翻訳済 (_call_api 内で raise された)
            raise
        except Exception as exc:
            translated = self._translate_error(exc)
            if translated is exc:
                raise  # bare re-raise — 自己 chain を避け、Task 層 UNKNOWN へ
            raise translated from exc
