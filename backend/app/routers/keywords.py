from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.dependencies import get_session
from app.models.associations import NewsKeyword
from app.models.keyword import Keyword
from app.schemas.keyword import KeywordCreate, KeywordResponse, KeywordUpdate

router = APIRouter(prefix="/api/v1/keywords", tags=["keywords"])


@router.get("", response_model=list[KeywordResponse])
async def list_keywords(
    session: AsyncSession = Depends(get_session),
) -> list[KeywordResponse]:
    stmt = select(Keyword).order_by(Keyword.created_at.desc())
    result = await session.execute(stmt)
    keywords = result.scalars().all()

    responses = []
    for kw in keywords:
        count_stmt = select(func.count()).where(
            NewsKeyword.keyword_id == kw.id
        )
        count_result = await session.execute(count_stmt)
        article_count = count_result.scalar_one()

        responses.append(
            KeywordResponse(
                id=kw.id,
                keyword=kw.keyword,
                category=kw.category,
                is_active=kw.is_active,
                article_count=article_count,
                created_at=kw.created_at,
            )
        )
    return responses


@router.post(
    "",
    response_model=KeywordResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_keyword(
    body: KeywordCreate,
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

    keyword = Keyword(keyword=body.keyword, category=body.category)
    session.add(keyword)
    await session.commit()
    await session.refresh(keyword)

    return KeywordResponse(
        id=keyword.id,
        keyword=keyword.keyword,
        category=keyword.category,
        is_active=keyword.is_active,
        article_count=0,
        created_at=keyword.created_at,
    )


@router.patch("/{keyword_id}", response_model=KeywordResponse)
async def update_keyword(
    keyword_id: int,
    body: KeywordUpdate,
    session: AsyncSession = Depends(get_session),
) -> KeywordResponse:
    keyword = await session.get(Keyword, keyword_id)
    if not keyword:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Keyword not found",
        )

    if body.is_active is not None:
        keyword.is_active = body.is_active
    keyword.updated_at = datetime.now(UTC)

    session.add(keyword)
    await session.commit()
    await session.refresh(keyword)

    count_stmt = select(func.count()).where(
        NewsKeyword.keyword_id == keyword.id
    )
    count_result = await session.execute(count_stmt)
    article_count = count_result.scalar_one()

    return KeywordResponse(
        id=keyword.id,
        keyword=keyword.keyword,
        category=keyword.category,
        is_active=keyword.is_active,
        article_count=article_count,
        created_at=keyword.created_at,
    )


@router.delete("/{keyword_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_keyword(
    keyword_id: int,
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
