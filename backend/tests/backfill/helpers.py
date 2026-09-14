"""backfillテスト用の工程到達済みデータと受付記録。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.outbox.publishing.publisher import BatchPublishResult, PublishSucceeded


@dataclass(frozen=True)
class SeededTarget:
    article_id: int
    target_id: int
    curation_id: int | None
    occurred_at: datetime


async def seed_target(session, source, category, stage, created_at):
    article = AnalyzableArticleRecord(
        source_id=source.id,
        source_url=f"https://example.com/{uuid4()}",
        original_title="title",
        original_content="content " * 20,
        published_at=created_at,
        created_at=created_at,
    )
    session.add(article)
    await session.flush()
    curation = None
    target_id = article.id
    occurred_at = created_at
    if stage in ("assessment", "embedding"):
        curation = ArticleCuration(
            analyzable_article_id=article.id,
            translated_title="title",
            summary="summary",
            extracted_at=created_at + timedelta(minutes=1),
        )
        session.add(curation)
        await session.flush()
        target_id, occurred_at = curation.id, curation.extracted_at
    if stage == "embedding":
        result = AnalyzedArticleRecord(
            curation_id=curation.id,
            translated_title="title",
            summary="summary",
            investor_take="take",
            category_id=category.id,
            analyzed_at=created_at + timedelta(minutes=2),
        )
        session.add(result)
        await session.flush()
        target_id, occurred_at = result.id, result.analyzed_at
    await session.commit()
    return SeededTarget(
        article.id, target_id, curation.id if curation else None, occurred_at
    )


async def complete_target(session, target, stage, category):
    if stage == "curation":
        session.add(
            ArticleCuration(
                analyzable_article_id=target.article_id,
                translated_title="title",
                summary="summary",
            )
        )
    elif stage == "assessment":
        session.add(
            AnalyzedArticleRecord(
                curation_id=target.target_id,
                translated_title="title",
                summary="summary",
                investor_take="take",
                category_id=category.id,
            )
        )
    else:
        result = await session.get(AnalyzedArticleRecord, target.target_id)
        result.embedding = [0.1] * 768
    await session.flush()


class RecordingPublisher:
    def __init__(self):
        self.batches = []

    def publish_batch(self, envelopes):
        self.batches.append(tuple(envelopes))
        return BatchPublishResult(
            tuple(PublishSucceeded(item.event_id) for item in envelopes)
        )

    @property
    def envelopes(self):
        return [item for batch in self.batches for item in batch]
