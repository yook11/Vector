"""GET /api/v1/briefing/{categorySlug} ルーター。

最新週の briefing を 1 リクエストで返す。``stories[].articleIds`` で参照される
記事 (title_ja / source_name / url) も同じレスポンスに同梱し、frontend で
N+1 fetch しないで済むようにする。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_optional_user, get_session
from app.insights.briefing.domain.headline import extract_first_sentence
from app.insights.briefing.domain.week import latest_completed_week_start, now_in_jst
from app.insights.briefing.repository.briefings import BriefingRepository
from app.insights.briefing.schemas.briefing import (
    BriefingListItem,
    BriefingListResponse,
    BriefingResponse,
    EmptyBriefing,
    ReadyBriefing,
    _ArticleSummaryOut,
    _BriefingListLatest,
    _CategoryOut,
    _StoryOut,
)
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource
from app.models.weekly_briefing import WeeklyBriefing

router = APIRouter(prefix="/api/v1/briefing", tags=["briefing"])


async def _fetch_category(session: AsyncSession, slug: str) -> Category:
    row = await session.execute(select(Category).where(Category.slug == slug))
    cat = row.scalar_one_or_none()
    if cat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"category not found: slug={slug}",
        )
    return cat


async def _fetch_article_summaries(
    session: AsyncSession, article_ids: list[int]
) -> list[_ArticleSummaryOut]:
    if not article_ids:
        return []
    # 公開 URL は ``Article.source_url`` (PR-E 以降は canonicalize 済み SSoT)。
    stmt = (
        select(
            Article.id,
            Article.source_url,
            InScopeAssessment.translated_title,
            NewsSource.name,
        )
        .join(ArticleExtraction, ArticleExtraction.article_id == Article.id)
        .join(
            InScopeAssessment,
            InScopeAssessment.extraction_id == ArticleExtraction.id,
        )
        .join(NewsSource, NewsSource.id == Article.source_id)
        .where(Article.id.in_(article_ids))
    )
    rows = (await session.execute(stmt)).all()
    return [
        _ArticleSummaryOut(
            id=row.id,
            title_ja=row.translated_title,
            source_name=str(row.name),
            url=str(row.source_url),
        )
        for row in rows
    ]


def _to_category(category: Category) -> _CategoryOut:
    return _CategoryOut(id=category.id, slug=category.slug, name=category.name)


@router.get("", dependencies=[Depends(get_optional_user)])
async def list_briefings(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BriefingListResponse:
    """全カテゴリの「ある中で最新」briefing を newspaper 一覧用に返す。

    items は ``Category.id`` 昇順で 11 カテゴリ全部を返す。未生成カテゴリは
    ``latest=None`` で表現する (frontend で 1 行 ``灰色`` 表示)。
    """
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
                    latest=_BriefingListLatest(
                        week_start=b.week_start_date,
                        headline_excerpt=extract_first_sentence(b.headline),
                    ),
                )
            )
    return BriefingListResponse(
        current_week_start=latest_completed_week_start(now_in_jst()),
        items=items,
    )


@router.get(
    "/{category_slug}",
    dependencies=[Depends(get_optional_user)],
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

    stories = [_StoryOut.model_validate(s) for s in briefing.stories]
    article_ids: list[int] = []
    for s in stories:
        article_ids.extend(s.article_ids)
    articles = await _fetch_article_summaries(session, list(set(article_ids)))

    return ReadyBriefing(
        week_start=briefing.week_start_date,
        generated_at=briefing.generated_at,
        model_name=briefing.model_name,
        input_article_count=briefing.input_article_count,
        category=_to_category(category),
        headline=briefing.headline,
        stories=stories,
        articles=articles,
    )
