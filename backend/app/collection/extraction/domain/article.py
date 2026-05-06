"""Article アグリゲート — Stage 0 で抽出された記事本文。

2 つの型で collection/extraction の概念を表す:

- ``ArticleDraft`` — AI 境界 (``ExtractedContent``) を sanitize 済みの
  ドメイン入力に正規化した型。永続化前の状態で、identity は持たない。
  ``from_extracted`` が AI 境界 → Draft の唯一の変換口。
- ``Article`` — システムに記録された記事 Entity。``id`` と
  ``discovered_article_id`` を identity として持ち、analysis 以降の処理が
  継続的に扱う概念。

変換は Repository.save (``ArticleDraft`` → ``Article``) と
Repository._to_domain (ORM → ``Article``) が担う。

定数 ``_ARTICLE_BODY_MIN_LENGTH`` / ``_ARTICLE_BODY_MAX_LENGTH`` は
本ファイルが SSoT。``extractor.py`` は import して品質ゲートで参照する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.collection.extraction.domain.value_objects import PublishedAt
from app.utils.sanitize import normalize_text

if TYPE_CHECKING:
    from app.collection.extraction.extractor import ExtractedContent


# Article 本文の長さ境界 (SSoT)。
# - min: 抽出器の品質ゲート閾値 (50 文字未満は ``ExtractionEmpty``)。
# - max: DoS 上限 (1MB)。日本語を考慮しても十分。
_ARTICLE_TITLE_MAX_LENGTH = 500
_ARTICLE_BODY_MIN_LENGTH = 50
_ARTICLE_BODY_MAX_LENGTH = 1_048_576


class ArticleDraft(BaseModel):
    """記事抽出結果のドメイン入力 (AI 境界 → 永続化前の正規化値)。

    ``ExtractedContent`` (extractor.py) を sanitize し、Pydantic の
    Field 制約 + validator で structural invariant を保証する。
    identity (``id`` / ``discovered_article_id``) はこの段階では未確定で、
    Service が ``Repository.save`` 呼び出し時に ``discovered_article_id`` を
    渡し、DB が採番した ``id`` / ``created_at`` と合わせて Entity を組み立てる。

    Invariants (validators / Field で構造的に保証):
    - ``title``: normalize 後 1..500 文字 (DB CHECK 制約 ``original_title != ''``)
    - ``body``: normalize 後 50..1_048_576 文字 (品質ゲート + DoS 上限)
    - ``published_at``: 任意 (記事によっては取得不能で妥当)
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=_ARTICLE_TITLE_MAX_LENGTH)
    body: str = Field(
        min_length=_ARTICLE_BODY_MIN_LENGTH, max_length=_ARTICLE_BODY_MAX_LENGTH
    )
    published_at: PublishedAt | None = None

    @field_validator("title", "body", mode="before")
    @classmethod
    def _sanitize(cls, v: Any) -> Any:
        """制御文字・null byte を除去し、Unicode を NFKC 正規化する。

        AI 境界 (HTML 抽出) からの混入を防ぐため、Field 検証より先に実行する。
        """
        if isinstance(v, str):
            return normalize_text(v)
        return v

    @field_validator("title", "body")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        """sanitize 後に空文字列となった場合を弾く防御層。

        ``Field(min_length=...)`` は最小長を構造的に保証するが、空文字列を
        ``ValueError`` 経路で確実に拒絶する明示的な保険として残す。
        """
        if not v:
            raise ValueError("must be non-empty after sanitization")
        return v

    @classmethod
    def from_extracted(cls, content: ExtractedContent) -> Self:
        """``ExtractedContent`` (AI 境界) を Draft に正規化する純粋変換。

        extractor は最低限の品質ゲート (length / 非空 title) を済ませているが、
        sanitize と DoS 上限はドメイン側の責務として Draft の validator が再検証する。
        """
        return cls(
            title=content.title,
            body=content.body,
            published_at=content.published_at,
        )


@dataclass(frozen=True, slots=True)
class Article:
    """システムに記録された記事 Entity。

    identity (``id``) と境界跨ぎ識別子を持ち、analysis 以降の Stage が
    ``id`` を入力に処理を継続する。

    境界跨ぎ識別子は PR2.5 移行期間中に並走する 2 系統:

    - ``discovered_article_id``: 旧経路 (PR2.5-A 以前)。新規 INSERT では
      NULL になり、旧 articles 行のみ値を保持。PR2.5-C で列削除予定。
    - ``article_url_id``: 新経路 (PR2.5-A 以降)。PR2.5-A backfill で旧 row も
      埋まり、新規 INSERT は必ずこれを持つ。PR2.5-C で NOT NULL 昇格予定。

    Invariants:
    - ``id`` は正の整数 (DB 採番)
    - 少なくとも ``discovered_article_id`` か ``article_url_id`` のいずれかは
      正の整数 (両方 NULL は invalid; PR2.5-A backfill で全 row が
      ``article_url_id`` を持つ)
    - ``title`` / ``body`` は非空 (DB CHECK 制約・品質ゲートと一致)
    - ``published_at`` は任意 (取得不能を許容)
    - ``created_at`` は ``server_default=func.now()`` で DB が採番した時刻
    """

    id: int
    discovered_article_id: int | None
    article_url_id: int | None
    title: str
    body: str
    published_at: PublishedAt | None
    created_at: datetime

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("Article.id must be positive")
        if self.discovered_article_id is None and self.article_url_id is None:
            raise ValueError(
                "Article must have either discovered_article_id or article_url_id"
            )
        if self.discovered_article_id is not None and self.discovered_article_id <= 0:
            raise ValueError("Article.discovered_article_id must be positive")
        if self.article_url_id is not None and self.article_url_id <= 0:
            raise ValueError("Article.article_url_id must be positive")
        if not self.title:
            raise ValueError("Article.title must be non-empty")
        if not self.body:
            raise ValueError("Article.body must be non-empty")

    @classmethod
    def from_draft(
        cls,
        draft: ArticleDraft,
        *,
        id: int,
        discovered_article_id: int,
        created_at: datetime,
    ) -> Self:
        """旧経路: ``discovered_article_id`` から Entity を組み立てる。

        PR2.5-B の cutover 完了で旧経路の caller (旧 ``IngestionService`` /
        旧 ``ContentFetchService``) が消えるため deprecated。新経路は
        ``from_draft_via_article_url`` を使う。
        """
        return cls(
            id=id,
            discovered_article_id=discovered_article_id,
            article_url_id=None,
            title=draft.title,
            body=draft.body,
            published_at=draft.published_at,
            created_at=created_at,
        )

    @classmethod
    def from_draft_via_article_url(
        cls,
        draft: ArticleDraft,
        *,
        id: int,
        article_url_id: int,
        created_at: datetime,
    ) -> Self:
        """新経路: ``article_url_id`` から Entity を組み立てる (PR2.5-B 以降)。

        Repository.save_via_article_url が成功した後、Service が呼び出して
        Outcome に詰めるためのドメインファクトリ。``discovered_article_id``
        は新規 INSERT では NULL なので Entity 上も NULL として扱う。
        """
        return cls(
            id=id,
            discovered_article_id=None,
            article_url_id=article_url_id,
            title=draft.title,
            body=draft.body,
            published_at=draft.published_at,
            created_at=created_at,
        )
