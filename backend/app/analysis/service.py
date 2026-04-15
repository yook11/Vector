"""Analysis service — orchestration and DB persistence."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.analyzer.base import AnalyzeResult, BaseAnalyzer
from app.analysis.analyzer.factory import get_analyzer
from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    DailyQuotaExhaustedError,
    RateLimitError,
)
from app.models.article_analysis import ArticleAnalysis
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.models.news_article import NewsArticle
from app.utils.sanitize import strip_html_tags

logger = structlog.get_logger(__name__)


async def analyze_article(
    session: AsyncSession,
    article: NewsArticle,
    analyzer: BaseAnalyzer | None = None,
) -> ArticleAnalysis | None:
    """Analyze a single news article and persist the result.

    Returns the created ArticleAnalysis, or None if already analyzed.

    Raises:
        AnalysisDomainError: If the AI provider fails. Callers using this
            function directly (outside analyze_articles) must handle
            this exception.
    """
    if analyzer is None:
        analyzer = get_analyzer()

    # Check if already analyzed (1:1 relationship)
    stmt = select(ArticleAnalysis).where(
        ArticleAnalysis.news_article_id == article.id,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "analysis_skipped",
            article_id=article.id,
            reason="already analyzed",
        )
        return None

    # Query all keyword candidates grouped by category
    keywords_by_category: dict[str, list[str]] | None = None
    kw_stmt = select(Category.slug, Keyword.name).join(
        Keyword, Keyword.category_id == Category.id
    )
    rows = (await session.execute(kw_stmt)).all()
    if rows:
        kw_dict: dict[str, list[str]] = {}
        for slug, kw in rows:
            kw_dict.setdefault(str(slug), []).append(str(kw))
        keywords_by_category = kw_dict

    try:
        data = await analyzer.analyze(
            title=article.original_title,
            description=article.original_description,
            content=article.original_content,
            keywords_by_category=keywords_by_category,
        )
    except AnalysisDomainError as e:
        logger.error("analysis_failed", article_id=article.id, error=str(e))
        raise

    # --- XSS対策: 多層防御 (Defense in Depth) ---
    # AI レスポンスは外部入力と同等に扱い、DB 永続化の直前でサニタイズする。
    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title=strip_html_tags(data.title) or "",
        summary=strip_html_tags(data.summary) or "",
        impact_level=data.impact_level,
        reasoning=strip_html_tags(data.reasoning) or "",
        ai_model=analyzer.model_name,
    )
    session.add(analysis)
    await session.flush()

    # Persist keyword links (AI-selected tags from keyword_candidates)
    if data.keywords:
        kw_stmt = select(Keyword).where(Keyword.name.in_(data.keywords))
        matched_kws = (await session.execute(kw_stmt)).scalars().all()
        for kw in matched_kws:
            link = ArticleKeyword(
                article_analysis_id=analysis.id,
                keyword_id=kw.id,
            )
            session.add(link)

    logger.info(
        "analysis_completed",
        article_id=article.id,
        impact_level=data.impact_level,
        keywords=data.keywords,
    )
    return analysis


async def analyze_articles(
    session: AsyncSession,
    articles: list[NewsArticle],
    analyzer: BaseAnalyzer | None = None,
) -> AnalyzeResult:
    """Analyze multiple articles sequentially.

    This function manages iteration, DB persistence, and error accumulation.

    AnalysisDomainError from individual articles is caught and accumulated
    in AnalyzeResult.errors so that the batch continues processing.
    """
    result = AnalyzeResult()

    if not articles:
        logger.info("analyze_batch_skipped", reason="no articles provided")
        return result

    if analyzer is None:
        analyzer = get_analyzer()

    # Detach articles so rollback/commit won't expire their attributes.
    for a in articles:
        session.expunge(a)

    for i, article in enumerate(articles):
        article_id = article.id

        try:
            analysis = await analyze_article(session, article, analyzer)
            if analysis is None:
                result.skipped_count += 1
            else:
                await session.commit()
                result.analyzed_count += 1
                logger.info("article_saved", article_id=article_id)
        except DailyQuotaExhaustedError as e:
            await session.rollback()
            remaining = len(articles) - i
            result.error_count += remaining
            result.errors.append(str(e))
            logger.warning(
                "analyze_daily_quota_exhausted",
                article_id=article_id,
                remaining=remaining,
                error=str(e),
            )
            break
        except ConfigurationError as e:
            await session.rollback()
            remaining = len(articles) - i
            result.error_count += remaining
            result.errors.append(str(e))
            logger.warning(
                "analyze_configuration_error",
                article_id=article_id,
                remaining=remaining,
                error=str(e),
            )
            break
        except RateLimitError as e:
            await session.rollback()
            result.error_count += 1
            result.errors.append(f"Article {article_id}: {e}")
            logger.warning(
                "analyze_batch_rate_limited",
                article_id=article_id,
                remaining=len(articles) - i - 1,
            )
            break
        except AnalysisDomainError as e:
            await session.rollback()
            result.error_count += 1
            result.errors.append(f"Article {article_id}: {e}")
            continue
        except Exception as e:
            await session.rollback()
            result.error_count += 1
            result.errors.append(f"Article {article_id}: {e}")
            logger.error(
                "article_analysis_unexpected_error",
                article_id=article_id,
                error=str(e),
            )
            continue

    logger.info(
        "analyze_batch_completed",
        analyzed=result.analyzed_count,
        skipped=result.skipped_count,
        errors=result.error_count,
    )
    return result


def _build_embed_text(article: NewsArticle) -> str:
    """Build the canonical text to embed for a news article."""
    body = article.original_content or article.original_description or ""
    return f"{article.original_title}\n{body}"
