"""GET /api/v1/briefing/{categorySlug} ルーター。

最新週の briefing を 1 リクエストで返す。``keyArticles[].article`` に参照記事
(translatedTitle / source / url / keyPoints) を埋め込み、frontend で
N+1 fetch しないで済むようにする。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session, require_bff_request
from app.insights.briefing.domain.briefing import MAX_KEY_ARTICLES_PER_BRIEFING
from app.insights.briefing.domain.week import latest_completed_week_start, now_in_jst
from app.insights.briefing.repository import BriefingRepository
from app.insights.briefing.schemas import (
    BriefingDetail,
    BriefingListItem,
    BriefingListResponse,
    BriefingResponse,
    BriefingSummary,
    EmptyBriefing,
    _BriefingArticleEmbed,
    _BriefingChapter,
    _BriefingKeyArticle,
)
from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource
from app.models.weekly_briefing import WeeklyBriefing
from app.schemas.embeds import CategoryEmbed, NewsSourceEmbed
from app.services.articles import extract_key_point_contents

router = APIRouter(prefix="/api/v1/briefing", tags=["briefing"])

# F10: 攻撃者が DB に直書きした巨大 key_articles で embed fetch / 組立が
# ガード発火より先に増幅しないよう、件数だけ先に検証する
# (上限・エラー形 = too_long ValidationError は BriefingDetail 側ガードと同一)。
_KEY_ARTICLES_COUNT_GUARD: TypeAdapter[list[object]] = TypeAdapter(
    Annotated[list[object], Field(max_length=MAX_KEY_ARTICLES_PER_BRIEFING)]
)


async def _fetch_category(session: AsyncSession, slug: str) -> Category:
    row = await session.execute(select(Category).where(Category.slug == slug))
    cat = row.scalar_one_or_none()
    if cat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"category not found: slug={slug}",
        )
    return cat


async def _fetch_article_embeds_by_assessment_id(
    session: AsyncSession, assessment_ids: set[int]
) -> dict[int, _BriefingArticleEmbed]:
    """key_articles (``assessment_id`` キー) が参照する記事の embed を返す。

    JSONB の assessment_id は公開 /news id 空間 (``InScopeAssessment.id``)
    そのものなので、dict キー = embed の公開 ``id`` で橋渡しが無い。
    """
    if not assessment_ids:
        return {}
    stmt = (
        select(
            InScopeAssessment.id,
            InScopeAssessment.translated_title,
            InScopeAssessment.key_points,
            Article.source_url,
            Article.published_at,
            NewsSource.name,
            NewsSource.attribution_label,
        )
        .join(ArticleCuration, ArticleCuration.id == InScopeAssessment.curation_id)
        .join(Article, Article.id == ArticleCuration.article_id)
        .join(NewsSource, NewsSource.id == Article.source_id)
        .where(InScopeAssessment.id.in_(assessment_ids))
    )
    rows = (await session.execute(stmt)).all()
    return {
        row.id: _BriefingArticleEmbed(
            id=row.id,
            translated_title=row.translated_title,
            source=NewsSourceEmbed(
                name=row.name,
                attribution_label=row.attribution_label,
            ),
            url=str(row.source_url),
            published_at=row.published_at,
            key_points=extract_key_point_contents(row.key_points),
        )
        for row in rows
    }


def _to_category(category: Category) -> CategoryEmbed:
    return CategoryEmbed(slug=category.slug, name=category.name)


@router.get("", dependencies=[Depends(require_bff_request)])
async def list_briefings(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BriefingListResponse:
    """全カテゴリの「ある中で最新」briefing を newspaper 一覧用に返す。

    items は ``Category.id`` 昇順で 11 カテゴリ全部を返す。未生成カテゴリは
    ``latest=None`` で表現する (frontend で 1 行 ``灰色`` 表示)。
    """
    current_week_start = latest_completed_week_start(now_in_jst())
    cats = (
        (await session.execute(select(Category).order_by(Category.id))).scalars().all()
    )
    repo = BriefingRepository(session)
    latest_by_cat = await repo.find_latest_for_each_category()

    items: list[BriefingListItem] = []
    for cat in cats:
        b = latest_by_cat.get(cat.id)
        if b is None:
            items.append(BriefingListItem(category=_to_category(cat), latest=None))
        else:
            items.append(
                BriefingListItem(
                    category=_to_category(cat),
                    latest=BriefingSummary(
                        week_start=b.week_start_date,
                        headline=b.headline,
                        summary=b.summary,
                        input_article_count=b.input_article_count,
                    ),
                )
            )
    # masthead「今週 N 件を解析」用。当該週に生成された briefing のみ数え、
    # 生成が遅れた古い週の stale briefing は今週の解析量に含めない。
    total_articles = sum(
        b.input_article_count
        for b in latest_by_cat.values()
        if b.week_start_date == current_week_start
    )
    return BriefingListResponse(
        current_week_start=current_week_start,
        total_articles=total_articles,
        items=items,
    )


@router.get(
    "/{category_slug}",
    dependencies=[Depends(require_bff_request)],
    responses={404: {"description": "category not found"}},
)
async def get_latest_briefing(
    category_slug: Annotated[
        str,
        Path(
            pattern=r"^[a-z0-9][a-z0-9_]{0,49}$",
            min_length=1,
            max_length=50,
        ),
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BriefingResponse:
    """指定カテゴリの最新 briefing を返す (なければ state="empty")。"""
    category = await _fetch_category(session, category_slug)
    repo = BriefingRepository(session)
    briefing: WeeklyBriefing | None = await repo.find_latest_by_category(
        category_id=category.id
    )
    if briefing is None:
        return EmptyBriefing(category=_to_category(category))

    chapters = [_BriefingChapter.model_validate(c) for c in briefing.chapters]
    _KEY_ARTICLES_COUNT_GUARD.validate_python(briefing.key_articles)
    embeds = await _fetch_article_embeds_by_assessment_id(
        session, {a["assessment_id"] for a in briefing.key_articles}
    )
    # article non-nullable は生成時 validator (article_id ⊆ assessed input_ids) と
    # 記事削除経路の不在が保証する (assessed 記事の retention 削除導入時は要見直し)。
    # embeds.get の None 欠落は non-nullable field の ValidationError → 500 で
    # loud に出す (failure_visibility)。
    key_articles = [
        _BriefingKeyArticle(
            significance=a["significance"],
            article=embeds.get(  # pyright: ignore[reportArgumentType]
                a["assessment_id"]
            ),
        )
        for a in briefing.key_articles
    ]
    watch_points = [w["statement"] for w in briefing.watch_points]

    return BriefingDetail(
        week_start=briefing.week_start_date,
        generated_at=briefing.generated_at,
        model_name=briefing.model_name,
        input_article_count=briefing.input_article_count,
        category=_to_category(category),
        headline=briefing.headline,
        summary=briefing.summary,
        chapters=chapters,
        key_articles=key_articles,
        watch_points=watch_points,
    )
