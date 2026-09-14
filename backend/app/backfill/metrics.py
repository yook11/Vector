"""新旧のbackfillで共有する件数の観測。"""

import logfire

age_deleted_counter = logfire.metric_counter(
    "vector.curation.age_deleted",
    unit="1",
    description="年齢起因 (7日超 child-NULL) で物理削除された article 数",
)
age_delete_batch_size_histogram = logfire.metric_histogram(
    "vector.curation.age_delete_batch_size",
    unit="1",
    description="1 cycle (cron) で年齢削除された記事の件数分布",
)
backlog_gauge = logfire.metric_gauge(
    "vector.backfill.backlog",
    unit="1",
    description="backfill が観測した DB 上の真の未処理件数 (stage 別)",
)
_dispatched_counter = logfire.metric_counter(
    "vector.backfill.dispatched",
    unit="1",
    description="backfill の送信先が受け付けた対象件数 (stage 別)",
)
_aged_out_counter = logfire.metric_counter(
    "vector.backfill.aged_out",
    unit="1",
    description="古すぎて通常 backfill から整理完了した対象件数 (stage/action 別)",
)


def record_dispatched(stage: str, count: int) -> None:
    """送信先の受付成功件数だけを記録する。"""
    if count:
        _dispatched_counter.add(count, attributes={"stage": stage})


def record_aged_out(stage: str, *, action: str, count: int) -> None:
    """整理のcommitが成功した件数だけを記録する。"""
    if count:
        _aged_out_counter.add(count, attributes={"stage": stage, "action": action})
