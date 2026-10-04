from typing import Annotated

from fastapi import APIRouter, Depends

from app.db.fastapi import EntryManagedSession
from app.dependencies import require_bff_request
from app.repositories.category import CategoryRepository
from app.schemas.category import CategoryStatsList
from app.services.category import CategoryService

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


def get_category_service(
    session: EntryManagedSession,
) -> CategoryService:
    return CategoryService(CategoryRepository(session))


@router.get("", dependencies=[Depends(require_bff_request)])
async def list_categories(
    service: Annotated[CategoryService, Depends(get_category_service)],
) -> CategoryStatsList:
    """全カテゴリと、直近 24 時間に分析された記事数を返す。

    レスポンスはユーザー非依存。BFF 経由証明を必須とし backend 直叩きを閉じるが、
    ログイン検証 (login gate) は BFF/Next.js が担うため user は要求しない。
    """
    return await service.list_category_stats()
