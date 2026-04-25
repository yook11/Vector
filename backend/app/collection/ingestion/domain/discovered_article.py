"""DiscoveredArticle アグリゲート — ingestion BC の中核ドメイン。

2 つの型で「外部ソースから発見された記事」の概念を表す:

- ``DiscoveredArticleDraft`` — 永続化前のドメイン入力 VO。
  ``ArticleCandidate`` (fetcher 境界の正規化値) を ``news_source_id`` と束ねた
  状態で、Service が ``Repository.save_many`` に渡すための入力形を表す。
- ``DiscoveredArticleEntity`` — システムに記録された Entity。identity (id) と
  発見時刻 (discovered_at) を持ち、下流 BC (collection.extraction) は
  ``id`` を入力に処理を進める。

変換は ``DiscoveredArticleDraft.from_candidate`` (candidate → Draft) と
``DiscoveredArticleEntity.from_draft`` (Draft + identity → Entity)、
Repository._to_domain (ORM → Entity) が担う。

NOTE: ORM クラス名 (``DiscoveredArticle``) との衝突を避けるため Entity 側は
``DiscoveredArticleEntity`` 接尾辞付きで定義する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from app.collection.ingestion.domain.value_objects import ArticleCandidate
from app.shared.value_objects.safe_url import SafeUrl

_TITLE_MAX_LENGTH = 500


class DiscoveredArticleDraft(BaseModel):
    """発見済み記事のドメイン入力 (永続化前の正規化値)。

    ``ArticleCandidate`` を ``news_source_id`` と束ねた状態。candidate 側で
    URL 安全性 + タイトル整形は済んでおり、Draft では ``news_source_id`` の
    正値性のみを構造的に保証する。

    Invariants:
    - ``candidate``: ``ArticleCandidate`` (URL 安全性 / タイトル整形済み)
    - ``news_source_id``: 正の整数 (strict, 暗黙 coerce 禁止)
    - frozen / strict: 生成後は不変、暗黙 coerce による型混入を弾く
    """

    model_config = ConfigDict(frozen=True, strict=True)

    candidate: ArticleCandidate
    news_source_id: int = Field(gt=0, strict=True)

    @classmethod
    def from_candidate(
        cls, candidate: ArticleCandidate, *, news_source_id: int
    ) -> Self:
        """fetcher が生成した候補に news_source_id を束ねて Draft を構築する。"""
        return cls(candidate=candidate, news_source_id=news_source_id)


@dataclass(frozen=True, slots=True, repr=False)
class DiscoveredArticleEntity:
    """システムに記録された発見済み記事 Entity。

    identity (id) と発見時刻 (discovered_at) を持つ。下流 BC
    (collection.extraction) は id を入力に処理を進める。

    Invariants:
    - ``id`` / ``news_source_id`` は正の整数
    - ``url`` は ``SafeUrl`` 型 (TypeDecorator 復元 + defense-in-depth)
    - ``title`` は非空 1..500 文字 (DB CHECK + ``String(500)`` と一致)

    NOTE: ``__repr__`` を override して URL / title を構造ログに展開させない。
    structlog や taskiq の中継経路で個人情報相当の文字列が漏れる経路を遮断する。
    """

    id: int
    news_source_id: int
    url: SafeUrl
    title: str
    discovered_at: datetime

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("DiscoveredArticleEntity.id must be positive")
        if self.news_source_id <= 0:
            raise ValueError("DiscoveredArticleEntity.news_source_id must be positive")
        if not self.title:
            raise ValueError("DiscoveredArticleEntity.title must be non-empty")
        if len(self.title) > _TITLE_MAX_LENGTH:
            raise ValueError(
                f"DiscoveredArticleEntity.title must be at most "
                f"{_TITLE_MAX_LENGTH} chars, got {len(self.title)}"
            )
        if not isinstance(self.url, SafeUrl):
            raise TypeError(
                "DiscoveredArticleEntity.url must be SafeUrl, "
                f"got {type(self.url).__name__}"
            )

    def __repr__(self) -> str:
        return (
            f"DiscoveredArticleEntity("
            f"id={self.id}, news_source_id={self.news_source_id})"
        )

    @classmethod
    def from_draft(
        cls,
        draft: DiscoveredArticleDraft,
        *,
        id: int,
        discovered_at: datetime,
    ) -> Self:
        """Draft に DB が付与した identity (``id`` / ``discovered_at``) を合成する。

        Repository.save_many が成功した行から Entity を組み立てるドメインファクトリ。
        ビジネスロジック変換はせず identity 合成のみ。
        """
        return cls(
            id=id,
            news_source_id=draft.news_source_id,
            url=draft.candidate.url,
            title=draft.candidate.title,
            discovered_at=discovered_at,
        )
