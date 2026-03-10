from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import func, select

from app.dependencies import get_admin_user, get_current_user, get_session
from app.models.associations import NewsKeyword
from app.models.keyword import Keyword
from app.models.keyword_category import (
    KeywordCategory,
    KeywordCategoryLink,
    KeywordCategoryTranslation,
)
from app.models.user import User
from app.schemas.keyword import (
    KeywordCreate,
    KeywordListResponse,
    KeywordResponse,
    KeywordUpdate,
)
from app.schemas.keyword_category import KeywordCategoryBrief

router = APIRouter(prefix="/api/v1/keywords", tags=["keywords"])


def _build_categories(
    category_links: list[KeywordCategoryLink], locale: str
) -> list[KeywordCategoryBrief]:
    """Extract translated category briefs from loaded category_links."""
    result = []
    for link in category_links:
        if not link.category:
            continue
        name = ""
        for t in link.category.translations:
            if t.locale == locale:
                name = t.name
                break
        result.append(KeywordCategoryBrief(slug=link.category.slug, name=name))
    return result


@router.get("", response_model=KeywordListResponse)
async def list_keywords(
    locale: str = Query("ja"),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> KeywordListResponse:
    stmt = (
        select(Keyword)
        .options(
            selectinload(Keyword.category_links)
            .selectinload(KeywordCategoryLink.category)
            .selectinload(KeywordCategory.translations)
        )
        .order_by(Keyword.created_at.desc())
    )
    result = await session.execute(stmt)
    keywords = result.unique().scalars().all()

    responses = []
    for kw in keywords:
        count_stmt = select(func.count()).where(NewsKeyword.keyword_id == kw.id)
        count_result = await session.execute(count_stmt)
        article_count = count_result.scalar_one()

        responses.append(
            KeywordResponse(
                id=kw.id,
                keyword=kw.keyword,
                categories=_build_categories(kw.category_links, locale),
                article_count=article_count,
                created_at=kw.created_at,
            )
        )
    return KeywordListResponse(items=responses)


@router.post(
    "",
    response_model=KeywordResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_keyword(
    body: KeywordCreate,
    _user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> KeywordResponse:
    existing = await session.execute(
        select(Keyword).where(Keyword.keyword == body.keyword)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Keyword already exists",
        )

    keyword = Keyword(keyword=body.keyword)
    session.add(keyword)
    await session.flush()

    categories: list[KeywordCategoryBrief] = []
    if body.category_ids:
        cat_stmt = (
            select(KeywordCategory)
            .options(selectinload(KeywordCategory.translations))
            .where(KeywordCategory.id.in_(body.category_ids))
        )
        cat_result = await session.execute(cat_stmt)
        valid_cats = {cat.id: cat for cat in cat_result.scalars().all()}

        for cat_id in body.category_ids:
            if cat_id not in valid_cats:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Category ID {cat_id} not found",
                )
            link = KeywordCategoryLink(keyword_id=keyword.id, category_id=cat_id)
            session.add(link)

        # Build response categories using loaded translations
        cat_translations = await session.execute(
            select(KeywordCategoryTranslation).where(
                KeywordCategoryTranslation.category_id.in_(body.category_ids),
                KeywordCategoryTranslation.locale == "ja",
            )
        )
        trans_map = {t.category_id: t.name for t in cat_translations.scalars().all()}
        categories = [
            KeywordCategoryBrief(
                slug=valid_cats[cid].slug,
                name=trans_map.get(cid, ""),
            )
            for cid in body.category_ids
            if cid in valid_cats
        ]

    await session.commit()
    await session.refresh(keyword)

    return KeywordResponse(
        id=keyword.id,
        keyword=keyword.keyword,
        categories=categories,
        article_count=0,
        created_at=keyword.created_at,
    )


@router.patch("/{keyword_id}", response_model=KeywordResponse)
async def update_keyword(
    keyword_id: int,
    body: KeywordUpdate,
    locale: str = Query("ja"),
    _user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> KeywordResponse:
    keyword = await session.get(Keyword, keyword_id)
    if not keyword:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Keyword not found",
        )

    if body.category_ids is not None:
        # Delete existing links
        existing_links_stmt = select(KeywordCategoryLink).where(
            KeywordCategoryLink.keyword_id == keyword_id
        )
        existing_links = (await session.execute(existing_links_stmt)).scalars().all()
        for link in existing_links:
            await session.delete(link)

        # Create new links
        if body.category_ids:
            cat_stmt = select(KeywordCategory).where(
                KeywordCategory.id.in_(body.category_ids)
            )
            cat_result = await session.execute(cat_stmt)
            valid_cats = {cat.id: cat for cat in cat_result.scalars().all()}
            for cat_id in body.category_ids:
                if cat_id not in valid_cats:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Category ID {cat_id} not found",
                    )
                link = KeywordCategoryLink(keyword_id=keyword_id, category_id=cat_id)
                session.add(link)

    keyword.updated_at = datetime.now(UTC)
    session.add(keyword)
    await session.commit()

    # Reload with categories for response
    stmt = (
        select(Keyword)
        .where(Keyword.id == keyword_id)
        .options(
            selectinload(Keyword.category_links)
            .selectinload(KeywordCategoryLink.category)
            .selectinload(KeywordCategory.translations)
        )
    )
    result = await session.execute(stmt)
    keyword = result.unique().scalar_one()

    count_stmt = select(func.count()).where(NewsKeyword.keyword_id == keyword.id)
    count_result = await session.execute(count_stmt)
    article_count = count_result.scalar_one()

    return KeywordResponse(
        id=keyword.id,
        keyword=keyword.keyword,
        categories=_build_categories(keyword.category_links, locale),
        article_count=article_count,
        created_at=keyword.created_at,
    )


@router.delete("/{keyword_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_keyword(
    keyword_id: int,
    _user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    keyword = await session.get(Keyword, keyword_id)
    if not keyword:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Keyword not found",
        )

    await session.delete(keyword)
    await session.commit()
