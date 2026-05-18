"""``FetchedArticle`` → ``AnalyzableArticle | ObservedArticle`` 変換 (純粋関数)。

per-source 責務は「body / published を信用できる形で渡せるか」のみに絞り、
Ready / Observed / 変換不能 の最終分岐は本変換器で一手に行う。同じ source
でも entry ごとに「Ready 昇格 / Observed 保留 / 変換不能」が決まる。

公開 API は ``convert_fetched_article`` ただ 1 つ。``ArticleFetcher`` +
``ArticleSource`` 経路から ``FetchedArticle`` を受け取り
``AnalyzableArticle`` | ``ObservedArticle`` に変換する。
本関数は private helper ``_convert_fetched_article`` に委譲し、
``AnalyzableArticle`` / ``ObservedArticle`` の直接構築箇所を 1 箇所に集約する。

判定順 (上から評価し、確定した時点で打ち切る):

1. title が空 / link が空 / link canonicalize 失敗 →
   ``FetchedArticleConversionError`` を raise (Analyzable / Observed どちらも
   不成立。両 reason に ``MISSING_TITLE`` / ``MISSING_URL`` / ``INVALID_URL``)
2. body が ``ARTICLE_BODY_MIN_LENGTH`` 以上 ``ARTICLE_BODY_MAX_LENGTH`` 以下、
   かつ ``published`` が有効な ``PublishedAt`` を組める、かつ profile が
   Stage-1 Ready を構造的に阻害しない → ``AnalyzableArticle`` を構築して返す
   - ``AnalyzableArticle`` の Pydantic 制約違反 → **drop せず** Observed
     fallback (recovery 性優先)。Ready 不成立理由は ``ANALYZABLE_INVARIANT``
3. それ以外 → ``ObservedArticle`` を構築して返す (**取れた事実は全部保存**:
   title / body / published_at を存在する限り ``ObservedField`` に詰める。
   要否 / 優先は Stage 2 で ``SourceCompletionProfile`` が決める)
4. ``ObservedArticle`` も Pydantic 制約違反 → ``FetchedArticleConversionError``
   を raise (``analyzable_reason`` = 段 2 での Ready 不成立理由、
   ``observed_reason=OBSERVED_BUILD_FAILED``)

profile のいずれかの analyzable field が ``html_preferred`` のとき (= その
field の正本は Stage-2 HTML 経由でしか確定しないプレースホルダ)、body /
published が揃っていても Ready 経路を止め ``ObservedArticle`` 保留に落とす
(HTML 補完で正本上書きの機会を残す安全弁)。判定は title hardcode ではなく
``SourceCompletionProfile.precludes_stage1_ready()`` への per-field 委譲で、
per-source の仮タイトル性を profile が所有する。

title trim は本変換器で集約 (``title.strip()[:ARTICLE_TITLE_MAX_LENGTH]``)。
per-source 側で ``entry.title[:500]`` を書く必要はない。

変換不能 entry は ``None`` で握りつぶさず ``FetchedArticleConversionError`` を
raise する純粋関数として表現する。stream を止めないための値化
(``ConversionRejection``) は ``ArticleFetcher`` の責務 (本モジュールで型だけ
定義し、変換器自身は値を返さず raise する)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
    ARTICLE_TITLE_MAX_LENGTH,
)
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.errors import (
    ConversionReason,
    FetchedArticleConversionError,
)
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
from app.shared.value_objects.source_name import SourceName


@dataclass(frozen=True, slots=True)
class ConversionRejection:
    """stream 境界で変換不能 entry を表す値。

    ``ArticleFetcher`` が ``FetchedArticleConversionError`` を捕捉して本値に
    変換し yield する。async generator から per-entry raise すると source
    stream 全体が止まる (恒久不良なら恒久停止) ため、例外を値に落として
    stream を継続させる。原因例外をそのまま内包し、Service の別 tx 監査が
    ``__cause__`` 連鎖まで辿れる。
    """

    error: FetchedArticleConversionError


def _ready_failure_reason(
    *,
    ready_precluded: bool,
    body_candidate: str | None,
    published_at: PublishedAt | None,
) -> ConversionReason:
    """段 2 (Ready 構築) が成立しなかった理由を 1 つに確定する。

    ゲートを上から評価し最初の不成立を返す。全ゲート通過なのに到達した場合
    (= ``can_build_ready`` が真で ``AnalyzableArticle`` 構築が Pydantic 制約で
    失敗) は ``ANALYZABLE_INVARIANT``。
    """
    if ready_precluded:
        return ConversionReason.READY_PRECLUDED
    if body_candidate is None:
        return ConversionReason.BODY_ABSENT
    if len(body_candidate) < ARTICLE_BODY_MIN_LENGTH:
        return ConversionReason.BODY_TOO_SHORT
    if len(body_candidate) > ARTICLE_BODY_MAX_LENGTH:
        return ConversionReason.BODY_TOO_LONG
    if published_at is None:
        return ConversionReason.PUBLISHED_ABSENT
    return ConversionReason.ANALYZABLE_INVARIANT


def _convert_fetched_article(
    *,
    title: str | None,
    link: str | None,
    body_candidate: str | None,
    published_hint: datetime | None,
    source_id: int,
    source_name: SourceName,
    origin: ObservedOrigin,
    ready_precluded: bool = False,
) -> AnalyzableArticle | ObservedArticle:
    """変換の共通実装 (private)。

    公開 API ``convert_fetched_article`` が委譲する単一の判定ロジック。
    ``AnalyzableArticle`` / ``ObservedArticle`` の直接構築はこの関数内のみで
    行い、構築箇所を 1 箇所に閉じ込める。どちらにも変換できない entry は
    ``FetchedArticleConversionError`` を raise する (``None`` は返さない)。
    """
    raw_url = link
    has_title = title is not None
    body_length = len(body_candidate) if body_candidate is not None else None
    has_published_at = published_hint is not None

    def _fail(
        analyzable_reason: ConversionReason,
        observed_reason: ConversionReason,
    ) -> FetchedArticleConversionError:
        return FetchedArticleConversionError(
            f"analyzable rejected: {analyzable_reason}; "
            f"observed rejected: {observed_reason}",
            analyzable_reason=analyzable_reason,
            observed_reason=observed_reason,
            source_name=str(source_name),
            raw_url=raw_url,
            has_title=has_title,
            body_length=body_length,
            has_published_at=has_published_at,
        )

    if title is None:
        raise _fail(
            ConversionReason.MISSING_TITLE, ConversionReason.MISSING_TITLE
        ) from None
    title_trimmed = title.strip()[:ARTICLE_TITLE_MAX_LENGTH]
    if not title_trimmed:
        raise _fail(
            ConversionReason.MISSING_TITLE, ConversionReason.MISSING_TITLE
        ) from None

    if not link:
        raise _fail(
            ConversionReason.MISSING_URL, ConversionReason.MISSING_URL
        ) from None
    try:
        source_url = CanonicalArticleUrl(link)
    except ValueError as err:
        raise _fail(ConversionReason.INVALID_URL, ConversionReason.INVALID_URL) from err

    # tz-naive datetime は published として採用しない (PublishedAt が拒否)。
    # 採用できなかった場合は Observed に published_at=None で流す。
    published_at: PublishedAt | None = None
    if published_hint is not None:
        try:
            published_at = PublishedAt(value=published_hint)
        except ValueError:
            published_at = None

    can_build_ready = (
        not ready_precluded
        and body_candidate is not None
        and ARTICLE_BODY_MIN_LENGTH <= len(body_candidate) <= ARTICLE_BODY_MAX_LENGTH
        and published_at is not None
    )
    if can_build_ready:
        try:
            return AnalyzableArticle(
                title=title_trimmed,
                body=body_candidate,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError:
            # Ready 二次的制約違反 (title sanitize 等の domain 側 invariant) は
            # Observed fallback で救う。変換不能には落とさない (recovery 性優先)。
            pass

    # 取れた事実は全部保存する (原則: 観測は全部保存し、要否は profile が決める)。
    # body は全現行ソースで html_required のため merge では無視されるが、観測
    # された事実としては保持する (forward-compat。挙動は不変 — spec §7 等価表)。
    try:
        return ObservedArticle(
            source_name=source_name,
            source_url=source_url,
            title=ObservedField(value=title_trimmed, origin=origin),
            body=(
                ObservedField(value=body_candidate, origin=origin)
                if body_candidate
                else None
            ),
            published_at=(
                ObservedField(value=published_at, origin=origin)
                if published_at is not None
                else None
            ),
        )
    except ValueError as err:
        raise _fail(
            _ready_failure_reason(
                ready_precluded=ready_precluded,
                body_candidate=body_candidate,
                published_at=published_at,
            ),
            ConversionReason.OBSERVED_BUILD_FAILED,
        ) from err


def convert_fetched_article(
    fetched: FetchedArticle,
    *,
    source: ArticleSource,
    source_id: int,
) -> AnalyzableArticle | ObservedArticle:
    """1 ``FetchedArticle`` を獲得型に変換する (Source 経路の唯一の変換器)。

    ``FetchedArticle`` の field は External boundary 層で空 str / ``None`` を
    用いた "不在" の表現を許容するため、本関数で str → ``None`` への正規化
    (空 str を不在シグナルに昇格) を行ってから ``_convert_fetched_article``
    に渡す。

    Source クラス属性 (``name`` / ``observed_origin`` / ``completion_profile``)
    を変換器へ thread する。profile のいずれかの analyzable field が
    ``html_preferred`` のとき (その field の正本は Stage-2 HTML でしか確定
    しない) Ready 経路を止める。判定は title hardcode ではなく
    ``profile.precludes_stage1_ready()`` への per-field 委譲で、source 固有
    flag を中間型に持たせない (R/H 分岐は不変)。

    Args:
        fetched: Source が外部 source から取り出した内部 DTO (ACL 境界)。
        source: 取得元 ``ArticleSource`` (identity / 補完方針の出所)。
        source_id: Stage 1 service が解決済の ``news_sources.id`` (Ready 経路の
            ``AnalyzableArticle`` が原産 FK として持つ。Observed 経路の
            identity は pending 行の関心で enqueue 時に注入される)。

    Returns:
        ``AnalyzableArticle`` — body + published_at が揃い品質ゲート通過。
        ``ObservedArticle`` — title + URL は揃うが Ready 条件を満たさない、
        または Ready 構築が Pydantic 制約で失敗した entry (取れた事実を全保存)。

    Raises:
        FetchedArticleConversionError: title / URL が無効で次工程に渡せない、
            または Observed 構築まで Pydantic 制約で失敗した entry。
            ``analyzable_reason`` / ``observed_reason`` に 2 ターゲットの
            不成立理由を載せ、原因例外を ``__cause__`` で連鎖する。
    """
    return _convert_fetched_article(
        title=fetched.title or None,
        link=fetched.url or None,
        body_candidate=fetched.body,
        published_hint=fetched.published_at,
        source_id=source_id,
        source_name=source.name,
        origin=source.observed_origin,
        ready_precluded=source.completion_profile.precludes_stage1_ready(),
    )
