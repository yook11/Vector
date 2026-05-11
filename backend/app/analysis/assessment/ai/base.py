"""API を単発呼び出しする抽象 Assessor 基底クラス。"""

from __future__ import annotations

import abc
from typing import ClassVar

import structlog

from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.schema import InScope, OutOfScope
from app.analysis.assessment.errors import AssessmentError
from app.analysis.errors.provider import AIProviderError

logger = structlog.get_logger(__name__)


class BaseAssessor(abc.ABC):
    """Stage 4 — Assessment のテンプレートメソッド基底。

    Stage 3 (Extraction) の構造化出力に対して判断を下す。原文は読まない。
    判定結果は ``AssessmentCall`` envelope (``result`` + 監査用 raw 情報) で返す
    (PR3 で `AssessmentResult` 直接返却から切り替え)。

    SDK 例外は ``_translate_error`` で ``AIProvider*Error`` (Stage 中立の
    Layer 2-A 識別 marker) に翻訳する。Stage 4 marker (``AssessmentError`` 系)
    への詰め替えは Service 層 ACL の責務であり、本 class は ``AIProvider*Error``
    段階で停止する (二重翻訳防止のため ``_call_once`` で素通し guard 済)。

    サブクラスは以下 3 つのフックを実装する:
    - ``assess``: プロンプト構築とレスポンス解析（公開 API）
    - ``_call_api``: SDK の生呼び出し → ``parse_assessment`` → ``AssessmentCall`` 構築
    - ``_translate_error``: SDK 例外を ``AIProvider*Error`` に翻訳する
      (マップ未知は ``return exc`` で caller の bare re-raise に委譲する規約)

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
    async def assess(
        self,
        title_ja: str,
        summary_ja: str,
    ) -> AssessmentCall[InScope] | AssessmentCall[OutOfScope]:
        """Stage 3 (Extraction) の出力を判定し ``AssessmentCall`` envelope を返す。

        Args:
            title_ja: 日本語翻訳タイトル。
            summary_ja: 事実ベースの日本語要約。

        Returns:
            ``AssessmentCall`` envelope。``result`` で ``InScope`` / ``OutOfScope``
            の tagged union を保持し、``raw_response`` / ``raw_category`` /
            ``raw_topic`` / ``prompt_version`` を audit 焼付用に運ぶ。

        Raises:
            AIProviderError: SDK 例外を ``_translate_error`` で翻訳した結果。
            AssessmentError: ``parse_assessment`` などの Stage 4 ACL が raise する
                schema 違反 (``AssessmentResponseInvalidError`` 等)。
            Exception: いずれにもマップできない未知例外 (bare re-raise)。
        """
        ...

    @abc.abstractmethod
    async def _call_api(
        self, prompt: str
    ) -> AssessmentCall[InScope] | AssessmentCall[OutOfScope]:
        """プロバイダー SDK を呼び出し、``AssessmentCall`` を返す。

        実装は SDK 応答を ``parse_assessment`` で詰め替え、``match`` で
        ``InScope`` / ``OutOfScope`` に narrow した上で ``AssessmentCall`` を
        構築する (戻り型は narrow された container の union)。
        """
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> Exception:
        """SDK 例外を ``AIProvider*Error`` (Stage 中立) に翻訳する。

        マップ可能なら対応する ``AIProvider*Error`` 派生 instance を返す。
        マップできなければ **入力 ``exc`` をそのまま返す** (caller である
        ``_call_once`` が ``if translated is exc: raise`` の bare re-raise guard
        で素通しする規約)。Stage 4 marker への詰め替えは Service 層 ACL の責務
        であり、本メソッドは ``AIProvider*Error`` 段階までで停止する。
        """
        ...

    # -- 単発呼び出し --

    async def _call_once(
        self, prompt: str
    ) -> AssessmentCall[InScope] | AssessmentCall[OutOfScope]:
        """1 回の API call。SDK 例外を ``AIProvider*Error`` 階層に翻訳して raise。

        Pattern:
        - 既に階層内 (``AIProviderError`` / ``AssessmentError``) の例外は **素通し**
          (二重翻訳防止)
        - それ以外は ``_translate_error`` 経由で翻訳。同じ exc が返ったら
          ``raise`` (from なし、UNKNOWN として catch-all 経路へ)
        - 翻訳された場合のみ ``raise translated from exc`` で原因連鎖
        """
        try:
            logger.info("assessor_api_call", model=self.model_name)
            result = await self._call_api(prompt)
            logger.info("assessor_api_success", model=self.model_name)
            return result
        except (AIProviderError, AssessmentError):
            # 既に階層内 (parse_assessment が raise した
            # AssessmentResponseInvalidError 等含む) — 二重翻訳防止
            raise
        except Exception as exc:
            translated = self._translate_error(exc)
            if translated is exc:
                # マップ未知 → catch-all (UNKNOWN 経路)、from を付けず素通し
                raise
            raise translated from exc
