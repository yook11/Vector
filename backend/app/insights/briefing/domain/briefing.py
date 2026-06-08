"""Briefing の LLM 出力 VO + ハルシネーション検証。

DeepSeek-V4 Pro が返す JSON を ``WeeklyBriefingContent`` として受ける。
``key_articles[].article_id`` が ``input_ids`` の subset であることを
``model_validator`` (mode="after") で検証し、捏造記事 id を含む応答を
構造的に弾く。

出力構造 (1 カテゴリ × 1 週):
- ``headline``: 週を一言で表す見出し
- ``summary``: 今週の総括 (リード文。headline 直後に置く数文の要旨)
- ``chapters``: 章 (``heading`` 見出し + ``body`` 本文) のリスト。
  本文を章立てしたストーリーとして構造化する (旧 ``overview`` 単一長文を置換)
- ``key_articles``: その中で特に重要な記事 (article_id + significance)
- ``watch_points``: 今後どこを見るべきか (観察すべき問い・論点。statement)

検証 context:
    ``WeeklyBriefingContent.model_validate(data, context={"input_ids": {1, 2, 3}})``
    のように context 経由で input_ids を渡す。LLM 呼出側 (DeepSeekBriefingGenerator)
    の責務。

サイズ上限 (red-team F10 構造防御):
    各 str / list の max_length は LLM 暴走 / prompt injection で巨大 briefing が
    DB に入る経路 (二次防御) を構造的に塞ぐ。anon GET 経路の防御は
    ``schemas/briefing.py`` の response schema 側で同値の max_length を持って
    実現する (router の model_validate 経由ではないため二箇所で持つ)。
"""

from __future__ import annotations

from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

# LLM 出力の現実的な上限。response schema (schemas/briefing.py) と同値で
# 持ち、二箇所で同じ振る舞いを保証する。
# - headline は短い見出し (一覧表示と詳細 header に同じものを使う)
# - summary は headline 直後の総括リード (数文)
# - chapters[].heading は章見出し、chapters[].body が週の流れ narrative を担う
# - key_articles[].significance は記事単位の「なぜ重要か」の短文
# - watch_points[].statement は「今後どこを見るべきか」の短文
MAX_BRIEFING_HEADLINE_LEN: Final[int] = 200
MAX_BRIEFING_SUMMARY_LEN: Final[int] = 1_000
MAX_CHAPTER_HEADING_LEN: Final[int] = 80
MAX_CHAPTER_BODY_LEN: Final[int] = 3_000
# 章数の上限ガード。章数は LLM 裁量 (下限 1) で、これは破綻防止の異常検知ライン。
MAX_CHAPTERS_PER_BRIEFING: Final[int] = 12
MAX_KEY_ARTICLE_SIGNIFICANCE_LEN: Final[int] = 600
# editorial 上限 (プロンプトの「最大 5 件」) とは別物の F10 異常検知ライン。
# 7 件程度の正常なばらつきは通し、injection / 暴走を疑う件数だけ loud に弾く。
MAX_KEY_ARTICLES_PER_BRIEFING: Final[int] = 20
MAX_WATCH_POINT_STATEMENT_LEN: Final[int] = 600
# watch_points も editorial (プロンプトの「1-3 件」) とは別の F10 異常検知ライン。
MAX_WATCH_POINTS_PER_BRIEFING: Final[int] = 8


class BriefingChapter(BaseModel):
    """本文を構成する章 1 つ = 見出し (heading) + 本文 (body)。

    headline 直後の ``summary`` とは別に、週のストーリーを章立てで構造化する。
    章数は LLM 裁量で、件数の下限/上限は ``WeeklyBriefingContent.chapters`` 側で持つ。
    """

    model_config = ConfigDict(frozen=True)

    heading: str = Field(min_length=1, max_length=MAX_CHAPTER_HEADING_LEN)
    body: str = Field(min_length=1, max_length=MAX_CHAPTER_BODY_LEN)


class KeyArticle(BaseModel):
    """その週で特に重要な記事 1 件 = 記事 id + なぜ重要か (significance)。"""

    model_config = ConfigDict(frozen=True)

    article_id: int
    significance: str = Field(min_length=1, max_length=MAX_KEY_ARTICLE_SIGNIFICANCE_LEN)


class WatchPoint(BaseModel):
    """今後どこを見るべきか = 観察すべき問い・論点 1 件。

    v1 では記事 id 接地を持たない (statement のみ)。段階 2 で
    ``basis_article_ids`` を additive に足せるよう、オブジェクト形で保持する。
    """

    model_config = ConfigDict(frozen=True)

    statement: str = Field(min_length=1, max_length=MAX_WATCH_POINT_STATEMENT_LEN)


class WeeklyBriefingContent(BaseModel):
    """LLM が返す 1 カテゴリ × 1 週分の briefing 全体。"""

    model_config = ConfigDict(frozen=True)

    headline: str = Field(min_length=1, max_length=MAX_BRIEFING_HEADLINE_LEN)
    summary: str = Field(min_length=1, max_length=MAX_BRIEFING_SUMMARY_LEN)
    chapters: list[BriefingChapter] = Field(
        min_length=1, max_length=MAX_CHAPTERS_PER_BRIEFING
    )
    key_articles: list[KeyArticle] = Field(
        min_length=1, max_length=MAX_KEY_ARTICLES_PER_BRIEFING
    )
    watch_points: list[WatchPoint] = Field(
        min_length=1, max_length=MAX_WATCH_POINTS_PER_BRIEFING
    )

    @model_validator(mode="after")
    def _reject_duplicate_key_article_ids(self) -> Self:
        """``key_articles`` の article_id 重複を構造的に弾く。

        LLM が同一記事を複数回挙げても DB 到達前に落とす。新 UI が
        ``articleId`` を React key に使うため一意性を保証する必要がある。
        件数とは独立した制約なので context 不要 (常時実行)。
        """
        ids = [ka.article_id for ka in self.key_articles]
        if len(ids) != len(set(ids)):
            raise ValueError(f"key_articles contain duplicate article_id: {ids}")
        return self

    @model_validator(mode="after")
    def _validate_article_ids_subset(self, info: ValidationInfo) -> Self:
        """``key_articles[].article_id ⊆ input_ids`` を保証する (ハルシネーション検出)。

        ``info.context["input_ids"]`` が指定されていない場合は検証をスキップする
        (テスト等で context を渡さない経路を許容する)。LLM 呼出経路では必ず
        ``input_ids`` を渡すこと。``watch_points`` は v1 では記事 id を持たないため
        検証対象外。
        """
        context = info.context
        if context is None:
            return self
        input_ids = context.get("input_ids")
        if input_ids is None:
            return self
        article_ids = {ka.article_id for ka in self.key_articles}
        unknown = article_ids - set(input_ids)
        if unknown:
            raise ValueError(
                f"key_articles contain ids not in input_ids: {sorted(unknown)}"
            )
        return self
