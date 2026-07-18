"""パイプライン操作（fetch）用の管理者エンドポイント。"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.pipeline.schemas import FetchRequest, FetchResponse
from app.dependencies import get_session
from app.models.news_source import NewsSource

router = APIRouter(prefix="/pipeline", tags=["admin:pipeline"])


@router.post(
    "/fetch",
    status_code=status.HTTP_202_ACCEPTED,
)
async def fetch_news(
    session: Annotated[AsyncSession, Depends(get_session)],
    body: FetchRequest | None = None,
) -> FetchResponse:
    """ニュース取得タスクを best-effort でキュー投入する。

    `source_ids` 指定時はソースごとに個別 task を dispatch し、未指定時は
    `dispatch_sources` で全 active source を dispatch する。`202 Accepted` と
    `dispatchedCount` は enqueue の受付のみを表し、実行・完了・耐久性を保証しない。

    - inactive source は cron で自動再投入されない。operator は request 時刻と
      source ID に対応する実行証跡を確認し、queue 滞留の解消後も証跡がなければ
      再実行する。
    - 再実行時も durable row の dedup は維持されるが、外部 HTTP 取得と新規記事の
      AI 処理は再発し得る。
    - multi-source の enqueue は非 atomic なループであり、失敗時は一部だけ
      enqueue 済みになり得る。
    - durable job ID / status の永続化は別 slice で扱う。
    """
    from app.queue.messages.collection import AcquireSourceTaskInput
    from app.queue.tasks.acquisition import acquire_source, dispatch_sources

    source_ids = body.source_ids if body else None

    if source_ids:
        # acquire_source は AcquireSourceTaskInput(id, name) envelope を要求するため、
        # 指定された source_id 群の name を 1 度の query で解決する。
        result = await session.execute(
            select(NewsSource.id, NewsSource.name).where(
                NewsSource.id.in_(source_ids)  # type: ignore[attr-defined]
            )
        )
        name_by_id: dict[int, str] = {row.id: str(row.name) for row in result}
        for sid in source_ids:
            name = name_by_id.get(sid)
            if name is None:
                continue
            await acquire_source.kiq(AcquireSourceTaskInput(id=sid, name=name))
        return FetchResponse(
            message="Fetch tasks submitted",
            dispatched_count=sum(1 for sid in source_ids if sid in name_by_id),
        )

    task = await dispatch_sources.kiq()
    return FetchResponse(
        message="Dispatch task submitted",
        job_id=task.task_id,
    )
