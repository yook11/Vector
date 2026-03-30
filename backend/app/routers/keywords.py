from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import func, select

from app.dependencies import CurrentUser, get_admin_user, get_current_user, get_session
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.schemas.embeds import CategoryEmbed
from app.schemas.keyword import (
    KeywordCreate,
    KeywordListResponse,
    KeywordResponse,
    KeywordUpdate,
)

router = APIRouter(prefix="/api/v1/keywords", tags=["keywords"])


@router.get("", response_model=KeywordListResponse)
async def list_keywords(
    _user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> KeywordListResponse:
    # Single query: keywords with category + article count via LEFT JOIN + GROUP BY
    stmt = (
        select(
            Keyword,
            func.count(ArticleKeyword.news_article_id).label("article_count"),
        )
        .outerjoin(ArticleKeyword, ArticleKeyword.keyword_id == Keyword.id)
        .options(selectinload(Keyword.category))
        .group_by(Keyword.id)
        .order_by(Keyword.created_at.desc())
    )
    result = await session.execute(stmt)
    rows = result.unique().all()

    return KeywordListResponse(
        items=[
            KeywordResponse(
                id=kw.id,
                name=kw.name,
                category=CategoryEmbed(
                    slug=kw.category.slug,
                    name=kw.category.name,
                ),
                status=kw.status,
                article_count=article_count,
                created_at=kw.created_at,
            )
            for kw, article_count in rows
        ]
    )


@router.post(
    "",
    response_model=KeywordResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_keyword(
    body: KeywordCreate,
    _user: CurrentUser = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> KeywordResponse:
    existing = await session.execute(select(Keyword).where(Keyword.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Keyword already exists",
        )

    # Verify category exists
    category = await session.get(Category, body.category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Category ID {body.category_id} not found",
        )

    keyword = Keyword(name=body.name, category_id=body.category_id)
    session.add(keyword)
    await session.commit()
    await session.refresh(keyword)

    return KeywordResponse(
        id=keyword.id,
        name=keyword.name,
        category=CategoryEmbed(slug=category.slug, name=category.name),
        status=keyword.status,
        article_count=0,
        created_at=keyword.created_at,
    )


@router.patch("/{keyword_id}", response_model=KeywordResponse)
async def update_keyword(
    keyword_id: int,
    body: KeywordUpdate,
    _user: CurrentUser = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> KeywordResponse:
    keyword = await session.get(Keyword, keyword_id)
    if not keyword:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Keyword not found",
        )

    if body.category_id is not None:
        category = await session.get(Category, body.category_id)
        if not category:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Category ID {body.category_id} not found",
            )
        keyword.category_id = body.category_id

    keyword.updated_at = datetime.now(UTC)
    session.add(keyword)
    await session.commit()

    # Reload with category + article count
    stmt = (
        select(
            Keyword,
            func.count(ArticleKeyword.news_article_id).label("article_count"),
        )
        .outerjoin(ArticleKeyword, ArticleKeyword.keyword_id == Keyword.id)
        .where(Keyword.id == keyword_id)
        .options(selectinload(Keyword.category))
        .group_by(Keyword.id)
    )
    result = await session.execute(stmt)
    row = result.unique().one()
    keyword, article_count = row

    return KeywordResponse(
        id=keyword.id,
        name=keyword.name,
        category=CategoryEmbed(
            slug=keyword.category.slug,
            name=keyword.category.name,
        ),
        status=keyword.status,
        article_count=article_count,
        created_at=keyword.created_at,
    )


@router.delete("/{keyword_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_keyword(
    keyword_id: int,
    _user: CurrentUser = Depends(get_admin_user),
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
