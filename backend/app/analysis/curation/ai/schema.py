"""Gemini SDK structured response 契約型 — Stage 3。

``response_schema=GeminiCurationResponse`` で Gemini に flat schema
(``{relevance, title_ja, summary_ja}``) を要求する SDK 契約型。

ドメイン層は ``Signal`` / ``Noise`` union (``CurationResult``) を使うが、
構造化出力で discriminated union を AI に要求すると精度が落ちる + Gemini SDK
の ``response_schema`` の制約により、AI 境界では flat 形式を維持する。
``ai/parse.py::parse_curation`` が ``relevance`` 値を見て domain 型に振り分ける。

設計詳細:
- PR2 以降は ``Field(description=...)`` を schema 側の field semantics SSoT として
  保持する (Gemini API は ``response_schema`` の description を生成プロンプトの
  一部として扱う)。``model_json_schema()`` は ``compute_call_signature`` の入力なので
  description 追加は ``VERSION`` を rotate させるが、これは「prompt + schema が
  実際に変わった」事実を表す **意図的な監査 cutover** として受け入れる。
- ``title="ExtractionResult"`` (ConfigDict) はクラス名差分による silent hash 変動を
  防ぐ別軸の固定で、Field description 追加と直交する。golden hash 固定は
  ``tests/analysis/curation/ai/test_gemini_prompt.py`` の
  ``test_version_locked_post_pr2`` 参照。**class rename 後も ``title`` は
  wire format として ``"ExtractionResult"`` を維持** (prompt_version hash 連続性)。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.sanitize import normalize_text


class GeminiCurationResponse(BaseModel):
    """AI が記事を分析した結果として期待するもの。

    Gemini の ``response_schema`` として使用されるため、フィールド名は
    AI プロンプトの規約に合わせて ``title_ja`` / ``summary_ja`` とする。

    ``relevance`` は Stage 1 signal/noise フィルタの判定結果。``"signal"``
    なら下流 (Stage 4 assessment) へ進み、``"noise"`` なら
    ``extraction_noises`` テーブルに記録して chain しない。

    Invariants (validators で構造的に保証):
    - ``relevance``: ``"signal"`` または ``"noise"``
    - ``title_ja`` / ``summary_ja``: HTML タグ除去 + NFKC + 制御文字除去後に非空
    - frozen: 生成後は不変

    BC 境界として下流 (Stage 4 Assessment) に「HTML 抜き、NFKC 済、制御文字無し、
    非空」を保証する責務を持つ。下流ステージで再 sanitize しない設計の前提
    (feedback_bc_boundary_guarantees_downstream)。
    """

    # ``title="ExtractionResult"`` is wire format kept — Gemini response_schema
    # の title 値はプロンプトハッシュの入力となるため、class rename 後も hash
    # 連続性のため変更しない (旧 Pydantic class が ``RESPONSE_SCHEMA`` だった頃の
    # hash 入力と bit-identical に保つ必要がある)。試験で固定済
    # (``tests/analysis/curation/ai/test_gemini_prompt.py``)。
    model_config = ConfigDict(frozen=True, title="ExtractionResult")

    relevance: Literal["signal", "noise"] = Field(
        description=(
            'Either "signal" or "noise". Choose "noise" ONLY when the article '
            "clearly does not help investment judgment or understanding world "
            'affairs. When uncertain, choose "signal".'
        )
    )
    title_ja: str = Field(
        description=(
            "Natural Japanese article title. Translate accurately from English, "
            "or clean up Japanese text without over-paraphrasing."
        )
    )
    summary_ja: str = Field(
        description=(
            "Fact-based Japanese summary covering important actors, actions, "
            "numbers, and technical novelty written in the article. Do not add "
            "facts that are not present in the article."
        )
    )

    @field_validator("title_ja", "summary_ja", mode="before")
    @classmethod
    def _sanitize(cls, v: Any) -> Any:
        """HTML タグ除去 + NFKC + 制御文字除去 + 前後空白トリム。"""
        if isinstance(v, str):
            return normalize_text(v) or ""
        return v

    @field_validator("title_ja", "summary_ja")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be non-empty after sanitization")
        return v
