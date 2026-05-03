"""ingestion BC の出口型 — `FetchedArticle` / `FetchedMetadata` / `FetchOutcome`。

collection-acquisition-redesign Phase 0c + Phase 1.0 + Phase 1b'。各 Fetcher
(per-source 実装) が返す出口を以下で固定する:

- ``FetchedArticle`` — 「articles に永続化可能な状態まで揃った」記事の VO。
  品質ゲート (title 非空 / body 50 文字以上 / published_at 必須) と
  source identity (source_id + source_url) を invariant として持つ。
  本クラスは strict 5 フィールドのみ。補足情報は同居しない。
- ``FetchedMetadata`` — capture-everything 用の補足情報 VO。全フィールド
  Optional、ソース毎に埋まり方が異なる (per-source coverage は
  ``Fetcher.PROVIDES`` で静的に宣言)。
- ``FetchOutcome = ReadyForArticle | PendingHtmlFetch | Failed`` — 1 entry
  の処理結果を sum 型で表現。
  ``ReadyForArticle`` は Article 永続化に進める passport (Pattern R Fetcher
  直接 / Pattern H fetch_html 完了時)。``PendingHtmlFetch`` は Pattern H 1
  段目の出口で、HTML 取得 task を起動するための中間 passport。``Failed`` は
  ``FailureReason`` を伴い、retry 可否と分類軸 (``code``) を上流 (Service 層)
  が一様に扱えるようにする。

Phase 1 以降の各 Fetcher は ``AsyncIterator[FetchOutcome]`` を返し、上流は
``match`` で ReadyForArticle / PendingHtmlFetch / Failed を分岐するだけで品質
ゲートも source identity も構造的に保証される
(`spec collection-acquisition-redesign.md §3` /
`spec collection-source-data-inventory.md §5`)。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.collection.extraction.domain.value_objects import PublishedAt
from app.shared.value_objects.safe_url import SafeUrl

_TITLE_MIN_LENGTH = 1
_TITLE_MAX_LENGTH = 500
_BODY_MIN_LENGTH = 50
_BODY_MAX_LENGTH = 1_048_576  # 1 MiB

# FetchedMetadata 用 max_length (防御的上限、ソース固有 quirk 由来の暴発防止)
_SUMMARY_MAX_LENGTH = 2000
_AUTHOR_MAX_LENGTH = 200
_LANGUAGE_MAX_LENGTH = 20
_GUID_MAX_LENGTH = 2048
_SITE_NAME_MAX_LENGTH = 100

FailureCode = Literal[
    "http_transient",
    "http_blocked",
    "paywalled",
    "extraction_empty",
    "body_too_short",
    "title_missing",
    "published_at_missing",
    "link_target_failed",
    "other",
]
"""``FetchOutcome.Failed`` の分類軸。

- ``http_transient``: 5xx / 429 / タイムアウト / DNS 一時失敗 (``retryable=True``)
- ``http_blocked``: 403 / 410 / 451 / robots.txt 拒否 (``retryable=False``)
- ``paywalled``: 有料記事と判定された (``retryable=False``)
- ``extraction_empty``: trafilatura パース不能 / Content-Type 不一致
  (``retryable=False``)
- ``body_too_short``: 本文が ``_BODY_MIN_LENGTH`` 未満 (``retryable=False``)
- ``title_missing``: タイトルが空 / 取得不能 (``retryable=False``)
- ``published_at_missing``: 公開日時を確定できなかった (``retryable=False``)
- ``link_target_failed``: HN のような link target に対する派生失敗 (``retryable=False``)
- ``other``: 上記いずれにも該当しない例外的なケース (``retryable=False``)
"""


class FailureReason(BaseModel):
    """``Failed`` が伴う失敗理由。``retryable`` は scheduler の再投入判定で参照する。

    ``detail`` は同 ``code`` を更に細分化する観察用文字列で、メトリクスや構造
    ログで活用する (例: ``code="published_at_missing"`` のとき
    ``detail="rss_pubdate_missing"`` / ``detail="trafilatura_no_date"`` /
    ``detail="date_parse_failed:<raw>"``)。
    """

    model_config = ConfigDict(frozen=True)

    code: FailureCode
    retryable: bool
    detail: str | None = None


class FetchedArticle(BaseModel):
    """articles に永続化可能な状態まで揃った記事の VO。**品質ゲート通過の証明**。

    Invariants (strict、全フィールド必須):
    - ``title``: 非空 1..500 文字 (DB ``original_title`` の ``String(500)`` と一致)
    - ``body``: 50..1_048_576 文字 (``ExtractedContent._BODY_MIN_LENGTH`` 等価)
    - ``published_at``: 必須 (旧 ``ArticleDraft`` の Optional から強化)
    - ``source_id``: 正の整数 (``news_sources.id`` への論理参照)
    - ``source_url``: ``SafeUrl`` (canonical URL、``articles.source_url`` UNIQUE 候補)

    旧 ``ArticleDraft`` との差分は (a) ``source_id`` / ``source_url`` の合成と
    (b) ``published_at`` の必須化。後者は ``published_at_missing`` を Fetcher 側で
    ``Failed`` に分岐させる前提で型レベルで強制する。

    補足情報 (summary / author / tags / image_url 等) は同居せず、姉妹型
    ``FetchedMetadata`` に分離する。本型の契約を「品質保証 = persistence
    可能」に限定するための意図的な分離 (`spec collection-source-data-inventory.md`).
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=_TITLE_MIN_LENGTH, max_length=_TITLE_MAX_LENGTH)
    body: str = Field(min_length=_BODY_MIN_LENGTH, max_length=_BODY_MAX_LENGTH)
    published_at: PublishedAt
    source_id: int = Field(gt=0)
    source_url: SafeUrl


class FetchedMetadata(BaseModel):
    """Fetcher が捕捉した補足情報 (capture-everything)。全フィールド Optional。

    各ソースで埋まり方が異なる (RSS / HTML / API のチャネル + ソース固有の
    availability)。**ソース毎に何を必ず提供するか** は ``Fetcher.PROVIDES``
    で静的に宣言する (per-Fetcher 実装の責務、本クラスは受け皿のみを定義)。

    Tier 設計 (`spec collection-source-data-inventory.md §5`):

    - Tier 1 = 専用フィールドとして昇格 (10 項目、本クラス)
    - Tier 2 = ``extras`` JSONB blob で温存 (将来カラム昇格候補)
    - Tier 3 = Fetcher 内部のみ、本クラスに含めない

    ``extras`` の運用範囲:

    入れて良いもの:
    - 部分カバレッジで将来のカラム昇格候補 (``slash:comments`` /
      ``wp_post_id`` / ``word_count`` / ``is_accessible_for_free`` /
      ``publisher_name`` 等)
    - HN の ``points`` / ``num_comments`` のような source-specific
      永続化候補

    入れてはいけないもの:
    - Tier 1 で受け止めるべき汎用フィールド
    - Fetcher 内部でのみ意味を持つ Tier 3 データ

    ``frozen=True`` は dict の reassignment を防ぐが、ネストした dict の
    mutate は防げない。caller は永続化前に deep-readonly 扱いとする規約。
    """

    model_config = ConfigDict(frozen=True)

    summary: str | None = Field(default=None, max_length=_SUMMARY_MAX_LENGTH)
    author: str | None = Field(default=None, max_length=_AUTHOR_MAX_LENGTH)
    authors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    image_url: SafeUrl | None = None
    language: str | None = Field(default=None, max_length=_LANGUAGE_MAX_LENGTH)
    guid: str | None = Field(default=None, max_length=_GUID_MAX_LENGTH)
    updated_at: PublishedAt | None = None
    site_name: str | None = Field(default=None, max_length=_SITE_NAME_MAX_LENGTH)
    extras: dict[str, Any] | None = None


class PendingHtmlFetch(BaseModel):
    """Pattern H (HTML 必須ソース) 1 段目の出口 = HTML fetch task 起動の passport。

    Pattern H ソース (TechCrunch / FierceBiotech / ITmedia 系等) は RSS が
    ``<description>`` にリード文しか出さず、本文は HTML を別途取得して
    trafilatura で抽出する必要がある。本型は **Stage A (RSS 取得) → Stage B
    (HTML 抽出)** の遷移を証明する中間 passport で、body と
    ``published_at`` の確定を後段に委ねる:

    - ``title``: RSS 確定値 (HTML より優先採用、merge 規則)
    - ``source_url``: ``SafeUrl`` で SSRF guard 通過済 (HTML fetch 先)
    - ``source_id``: ``news_sources.id`` 参照
    - ``published_at_hint``: RSS の ``pubDate`` (HTML 補完前、欠落 OK)
    - ``metadata``: capture-everything (RSS から救出された補足情報)

    後段 ``ReadyForArticle.try_advance_from(pending, html)`` で HTML 抽出
    結果と merge され、``FetchedArticle`` (body 込) を構築する。Pattern R
    との違い: Pattern R は ``ReadyForArticle`` を Fetcher が直接 yield する
    (HTML 抽出が不要)。

    本型は ``StagedArticle`` (kiq message) に乗って worker 間を運ばれるため
    Pydantic ``BaseModel(frozen=True)`` 必須 (memory
    `feedback_taskiq_basemodel_required.md`)。

    ``prefer_html_title`` は sitemap 系ソース (RSS が title を一切提供しない)
    のためのオプトイン flag。``True`` のとき ``try_advance_from`` の merge は
    HTML 抽出由来の title を採用する。デフォルト (RSS ソース全般、Anthropic
    以外) では従来通り RSS 由来の ``title`` を採用する。
    ``title`` 自身は ``min_length=1`` を満たす必要があるため、sitemap 系
    Fetcher は URL slug 等のプレースホルダ文字列を入れる (HTML 抽出成功時に
    overwrite され、失敗時は記事ごと drop されるためプレースホルダは永続化
    されない)。
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=_TITLE_MIN_LENGTH, max_length=_TITLE_MAX_LENGTH)
    source_id: int = Field(gt=0)
    source_url: SafeUrl
    published_at_hint: PublishedAt | None = None
    metadata: FetchedMetadata = Field(default_factory=FetchedMetadata)
    prefer_html_title: bool = False


class ReadyForArticle(BaseModel):
    """Article 永続化に進める passport (= ``FetchOutcome`` の最終成功形)。

    意味: 「``articles`` 行を作成し、Stage C (extract_content) に進める前提
    条件をすべて満たしている」状態の証明。生成箇所は 2 系統:

    1. **Pattern R Fetcher** が直接 yield する (RSS 1 段で完結、本文込み)
    2. **Pattern H** で ``extract_html_body`` task が
       ``try_advance_from(pending, html)`` で構築する (HTML 抽出後)

    どちらの経路でも消費先 (= ``IngestionService`` / task の Article 永続化
    分岐) は同一なので、型として 1 つに集約する (memory
    `project_typed_pipeline_preconditions.md`)。

    ``article`` が品質ゲート通過の証明、``metadata`` が補足情報。Fetcher は
    article のみ必須で、metadata は省略可能 (空の ``FetchedMetadata()``
    がデフォルト)。
    """

    model_config = ConfigDict(frozen=True)

    article: FetchedArticle
    metadata: FetchedMetadata = Field(default_factory=FetchedMetadata)

    @classmethod
    def try_advance_from(
        cls,
        pending: PendingHtmlFetch,
        body: str,
        html_published_at: PublishedAt | None,
        html_title: str | None = None,
    ) -> ReadyForArticle | Failed:
        """Pattern H で HTML 抽出成功後、``PendingHtmlFetch`` + HTML 結果から昇格。

        Merge 規則 (RSS 優先 / HTML 補完):

        - ``title``: ``pending.prefer_html_title`` が ``True`` かつ
          ``html_title`` があれば HTML 由来を採用 (sitemap 系ソース向け)。
          それ以外は ``pending.title`` (RSS 確定値) を採用。
        - ``body``: ``body`` (HTML 抽出結果、HTML only)
        - ``published_at``: ``pending.published_at_hint or html_published_at``
          (RSS が出していれば優先、欠落時のみ HTML フォールバック)
        - ``source_id`` / ``source_url``: ``pending`` から (RSS 確定)
        - ``metadata``: ``pending.metadata`` そのまま (HTML 由来 metadata は
          現状利用しない、必要になれば本シグネチャを拡張する)

        Failed 降格条件:

        - ``published_at`` が RSS / HTML 両方とも欠落
        - ``FetchedArticle`` invariant 違反 (body 50 文字未満 / title 500
          字超等、構築時に ``ValueError`` を捕捉して降格)

        引数を ``HtmlExtractionResult`` 型ではなく素の ``body`` /
        ``html_published_at`` / ``html_title`` で受けるのは
        ``app.collection.extraction`` への循環 import を避けるため。呼出側
        (extract_html_body task) で ``ExtractedContent`` から取り出して渡す。
        """
        final_published = pending.published_at_hint or html_published_at
        if final_published is None:
            return Failed(
                reason=FailureReason(
                    code="published_at_missing",
                    retryable=False,
                    detail="rss_and_html_both_missing",
                )
            )
        final_title = (
            html_title if (pending.prefer_html_title and html_title) else pending.title
        )
        try:
            article = FetchedArticle(
                title=final_title,
                body=body,
                published_at=final_published,
                source_id=pending.source_id,
                source_url=pending.source_url,
            )
        except ValueError as e:
            return Failed(
                reason=FailureReason(
                    code="other",
                    retryable=False,
                    detail=f"invariant_violation:{e}",
                )
            )
        return cls(article=article, metadata=pending.metadata)


class Failed(BaseModel):
    """``FetchOutcome`` の失敗側。理由は ``FailureReason`` で構造化する。"""

    model_config = ConfigDict(frozen=True)

    reason: FailureReason


FetchOutcome = ReadyForArticle | PendingHtmlFetch | Failed
"""1 entry の Fetcher 結果。

3 variants の意味:

- ``ReadyForArticle``: Article 永続化に進める (Pattern R Fetcher 直接 /
  Pattern H ``extract_html_body`` task 完了時)
- ``PendingHtmlFetch``: Pattern H 1 段目で HTML fetch task に橋渡しする
  中間状態
- ``Failed``: 品質ゲート違反 / 永続的失敗 (retryable は ``FailureReason`` 経由)

discriminator tag は持たない単純な Union: in-process では ``match`` 分岐、
taskiq message では ``StagedArticle`` 経由で ``PendingHtmlFetch`` のみ運ぶ
(``ReadyForArticle`` は kiq に乗らない)。
"""
