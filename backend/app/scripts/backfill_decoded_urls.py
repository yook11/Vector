"""Backfill decoded URLs for existing articles with Google News redirect URLs.

One-time script to fix articles stored with Google News encoded URLs.
Decodes each URL to the real article URL, handles UNIQUE conflicts by merging
keyword links, and resets content fields so the worker re-fetches content.

Usage:
    docker compose exec backend python -m app.scripts.backfill_decoded_urls
    docker compose exec backend python -m app.scripts.backfill_decoded_urls --dry-run
"""

import argparse
import asyncio
import sys

import structlog
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.db import engine
from app.models.associations import NewsKeyword
from app.models.news import NewsArticle
from app.services.url_decoder import decode_urls, is_google_news_url

logger = structlog.get_logger(__name__)


async def backfill(dry_run: bool = False) -> None:
    """Decode Google News URLs in existing articles and reset content fields."""
    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
        # Find all articles with Google News URLs
        stmt = select(NewsArticle).where(
            NewsArticle.url.like("https://news.google.com/%")
        )
        result = await session.execute(stmt)
        articles = list(result.scalars().all())

        if not articles:
            print("No articles with Google News URLs found.")
            return

        print(f"Found {len(articles)} articles with Google News URLs.")

        # Batch decode all URLs
        urls = [a.url for a in articles]
        url_mapping = await decode_urls(urls, interval=1.0)

        updated = 0
        merged = 0
        failed = 0

        for article in articles:
            decoded_url = url_mapping.get(article.url, article.url)

            if decoded_url == article.url or is_google_news_url(decoded_url):
                # Decode failed or returned another Google URL
                failed += 1
                print(f"  SKIP  id={article.id} — decode failed")
                continue

            # Check if decoded URL already exists in DB (UNIQUE conflict)
            existing_stmt = select(NewsArticle).where(
                NewsArticle.url == decoded_url,
                NewsArticle.id != article.id,
            )
            existing_result = await session.execute(existing_stmt)
            existing_article = existing_result.scalar_one_or_none()

            if existing_article:
                # Merge: transfer keyword links from duplicate to existing
                links_stmt = select(NewsKeyword).where(
                    NewsKeyword.news_article_id == article.id
                )
                links_result = await session.execute(links_stmt)
                links = list(links_result.scalars().all())

                for link in links:
                    # Check if existing article already has this keyword link
                    dup_check = await session.execute(
                        select(NewsKeyword).where(
                            NewsKeyword.news_article_id == existing_article.id,
                            NewsKeyword.keyword_id == link.keyword_id,
                        )
                    )
                    if not dup_check.scalar_one_or_none():
                        new_link = NewsKeyword(
                            news_article_id=existing_article.id,
                            keyword_id=link.keyword_id,
                        )
                        session.add(new_link)

                if not dry_run:
                    await session.delete(article)
                merged += 1
                print(
                    f"  MERGE id={article.id} -> id={existing_article.id} "
                    f"url={decoded_url[:80]}"
                )
            else:
                # Update URL and reset content fields for re-fetch
                if not dry_run:
                    article.url = decoded_url
                    article.content = None
                    article.content_fetched_at = None
                    session.add(article)
                updated += 1
                print(f"  UPDATE id={article.id} url={decoded_url[:80]}")

        if not dry_run:
            await session.commit()
            print(f"\nCommitted: {updated} updated, {merged} merged, {failed} failed.")
        else:
            print(
                f"\n[DRY RUN] Would update {updated}, merge {merged}, "
                f"skip {failed}. No changes made."
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill decoded URLs for Google News articles."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying the database.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(backfill(dry_run=args.dry_run))
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
