"""Investigate cosine similarity distribution among news articles.

Connects to the Vector database and analyzes embedding distances to help
determine an appropriate threshold for duplicate article detection (3B-1).

Usage:
    cd backend && python scripts/investigate_similarity.py

Requires DATABASE_URL env var or .env file in backend/.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

# Load .env from backend directory
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env() -> None:
    """Minimal .env loader — only reads DATABASE_URL if not already set."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key not in os.environ:
            os.environ[key] = value


def _get_dsn() -> str:
    """Return a PostgreSQL DSN (asyncpg format) from DATABASE_URL."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("ERROR: DATABASE_URL not set. Check .env or environment.", file=sys.stderr)
        sys.exit(1)
    # Convert SQLAlchemy URL to asyncpg DSN
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def run() -> None:
    _load_env()
    dsn = _get_dsn()
    conn = await asyncpg.connect(dsn)

    try:
        # -----------------------------------------------------------
        # 0. Basic stats
        # -----------------------------------------------------------
        total = await conn.fetchval("SELECT count(*) FROM news_articles")
        with_emb = await conn.fetchval(
            "SELECT count(*) FROM news_articles WHERE embedding IS NOT NULL"
        )
        print(f"\n=== Basic Stats ===")
        print(f"Total articles:           {total}")
        print(f"Articles with embedding:  {with_emb}")
        if with_emb < 2:
            print("Not enough articles with embeddings to analyze. Exiting.")
            return

        # -----------------------------------------------------------
        # 1. Top-20 closest pairs (cross-source, within 3 days)
        # -----------------------------------------------------------
        print(f"\n=== Top 20 Closest Pairs (different sources, within 3 days) ===")
        top_pairs = await conn.fetch("""
            SELECT
                a.id AS id_a,
                b.id AS id_b,
                a.title_original AS title_a,
                b.title_original AS title_b,
                a.source AS source_a,
                b.source AS source_b,
                a.published_at AS pub_a,
                b.published_at AS pub_b,
                (a.embedding <=> b.embedding) AS distance
            FROM news_articles a
            JOIN news_articles b
                ON b.id > a.id
                AND b.embedding IS NOT NULL
                AND a.source_id IS DISTINCT FROM b.source_id
                AND b.published_at BETWEEN a.published_at - INTERVAL '3 days'
                                       AND a.published_at + INTERVAL '3 days'
            WHERE a.embedding IS NOT NULL
              AND a.published_at IS NOT NULL
              AND b.published_at IS NOT NULL
            ORDER BY distance
            LIMIT 20
        """)

        for i, row in enumerate(top_pairs, 1):
            print(f"\n--- Pair {i} (distance: {row['distance']:.4f}) ---")
            print(f"  [{row['source_a']}] {row['title_a'][:80]}")
            print(f"  [{row['source_b']}] {row['title_b'][:80]}")

        # -----------------------------------------------------------
        # 2. Distance histogram (cross-source, within 3 days)
        # -----------------------------------------------------------
        print(f"\n=== Distance Distribution (cross-source, within 3 days) ===")
        print(f"{'Range':<16} {'Pairs':>10}")
        print("-" * 28)

        histogram = await conn.fetch("""
            WITH pair_distances AS (
                SELECT (a.embedding <=> b.embedding) AS distance
                FROM news_articles a
                JOIN news_articles b
                    ON b.id > a.id
                    AND b.embedding IS NOT NULL
                    AND a.source_id IS DISTINCT FROM b.source_id
                    AND b.published_at BETWEEN a.published_at - INTERVAL '3 days'
                                           AND a.published_at + INTERVAL '3 days'
                WHERE a.embedding IS NOT NULL
                  AND a.published_at IS NOT NULL
                  AND b.published_at IS NOT NULL
            )
            SELECT
                bucket,
                count(*) AS pair_count
            FROM (
                SELECT
                    width_bucket(distance, 0.0, 0.5, 10) AS bucket
                FROM pair_distances
            ) sub
            GROUP BY bucket
            ORDER BY bucket
        """)

        bucket_labels = [
            "0.00 - 0.05",
            "0.05 - 0.10",
            "0.10 - 0.15",
            "0.15 - 0.20",
            "0.20 - 0.25",
            "0.25 - 0.30",
            "0.30 - 0.35",
            "0.35 - 0.40",
            "0.40 - 0.45",
            "0.45 - 0.50",
            ">= 0.50",
        ]
        for row in histogram:
            bucket_idx = row["bucket"]
            if 1 <= bucket_idx <= 10:
                label = bucket_labels[bucket_idx - 1]
            else:
                label = bucket_labels[-1]
            print(f"  {label:<14} {row['pair_count']:>10,}")

        # -----------------------------------------------------------
        # 3. Same-source vs cross-source comparison
        # -----------------------------------------------------------
        print(f"\n=== Same Source vs Cross Source (within 3 days, distance < 0.3) ===")

        comparison = await conn.fetch("""
            WITH pairs AS (
                SELECT
                    (a.source_id = b.source_id) AS same_source,
                    (a.embedding <=> b.embedding) AS distance
                FROM news_articles a
                JOIN news_articles b
                    ON b.id > a.id
                    AND b.embedding IS NOT NULL
                    AND b.published_at BETWEEN a.published_at - INTERVAL '3 days'
                                           AND a.published_at + INTERVAL '3 days'
                WHERE a.embedding IS NOT NULL
                  AND a.published_at IS NOT NULL
                  AND b.published_at IS NOT NULL
                  AND (a.embedding <=> b.embedding) < 0.3
            )
            SELECT
                same_source,
                count(*) AS pair_count,
                avg(distance) AS avg_distance,
                min(distance) AS min_distance,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY distance) AS median_distance
            FROM pairs
            GROUP BY same_source
            ORDER BY same_source
        """)

        for row in comparison:
            label = "Same source" if row["same_source"] else "Cross source"
            print(f"\n  {label}:")
            print(f"    Pairs:    {row['pair_count']:,}")
            print(f"    Avg dist: {row['avg_distance']:.4f}")
            print(f"    Min dist: {row['min_distance']:.4f}")
            print(f"    Median:   {row['median_distance']:.4f}")

        # -----------------------------------------------------------
        # 4. Candidate duplicate counts at various thresholds
        # -----------------------------------------------------------
        print(f"\n=== Candidate Duplicates at Various Thresholds (cross-source, 3 days) ===")

        thresholds = await conn.fetch("""
            WITH pair_distances AS (
                SELECT (a.embedding <=> b.embedding) AS distance
                FROM news_articles a
                JOIN news_articles b
                    ON b.id > a.id
                    AND b.embedding IS NOT NULL
                    AND a.source_id IS DISTINCT FROM b.source_id
                    AND b.published_at BETWEEN a.published_at - INTERVAL '3 days'
                                           AND a.published_at + INTERVAL '3 days'
                WHERE a.embedding IS NOT NULL
                  AND a.published_at IS NOT NULL
                  AND b.published_at IS NOT NULL
                  AND (a.embedding <=> b.embedding) < 0.25
            )
            SELECT
                threshold,
                count(*) FILTER (WHERE distance < threshold) AS pair_count
            FROM pair_distances
            CROSS JOIN (VALUES (0.05), (0.08), (0.10), (0.12), (0.15), (0.18), (0.20), (0.25)) AS t(threshold)
            GROUP BY threshold
            ORDER BY threshold
        """)

        print(f"  {'Threshold':<12} {'Pairs':>10}")
        print("  " + "-" * 24)
        for row in thresholds:
            print(f"  < {row['threshold']:<9.2f} {row['pair_count']:>10,}")

        print("\n=== Done ===\n")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
