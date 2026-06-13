"""Stage 4 article persistence DB naming contract.

This file intentionally fixes the PR2 database boundary:
``analyzed_articles`` / ``out_of_scope_articles`` are the durable table names,
and foreign keys into ``analyzed_articles.id`` use the explicit
``analyzed_article_id`` column/attribute name.
"""

from __future__ import annotations

from pathlib import Path

import app.models as _models  # noqa: F401  # populate Base.metadata
from app.models.article_curation import ArticleCuration
from app.models.backfill_exclusion import EmbeddingBackfillExclusion
from app.models.base import Base
from app.models.watchlist_entry import WatchlistEntry


def _fk_targets(table_name: str, column_name: str) -> set[str]:
    column = Base.metadata.tables[table_name].c[column_name]
    return {fk.target_fullname for fk in column.foreign_keys}


def _constraint_names(table_name: str) -> set[str | None]:
    return {
        constraint.name for constraint in Base.metadata.tables[table_name].constraints
    }


def _index_names(table_name: str) -> set[str]:
    return {index.name for index in Base.metadata.tables[table_name].indexes}


def test_stage4_article_tables_use_state_names() -> None:
    tables = set(Base.metadata.tables)

    assert "analyzed_articles" in tables
    assert "out_of_scope_articles" in tables
    assert "in_scope_assessments" not in tables
    assert "out_of_scope_assessments" not in tables


def test_analyzed_articles_table_contract() -> None:
    table = Base.metadata.tables["analyzed_articles"]

    assert set(table.columns.keys()) == {
        "id",
        "curation_id",
        "translated_title",
        "summary",
        "investor_take",
        "analyzed_at",
        "embedding",
        "category_id",
        "key_points",
    }
    assert _fk_targets("analyzed_articles", "curation_id") == {"article_curations.id"}
    assert _fk_targets("analyzed_articles", "category_id") == {"categories.id"}
    assert {
        "uq_analyzed_articles_curation_id",
        "ck_analyzed_articles_translated_title_not_empty",
        "ck_analyzed_articles_summary_not_empty",
        "ck_analyzed_articles_investor_take_not_empty",
    }.issubset(_constraint_names("analyzed_articles"))
    assert {
        "ix_analyzed_articles_category_id_analyzed_at",
    }.issubset(_index_names("analyzed_articles"))


def test_out_of_scope_articles_table_contract() -> None:
    table = Base.metadata.tables["out_of_scope_articles"]

    assert set(table.columns.keys()) == {
        "id",
        "curation_id",
        "translated_title",
        "summary",
        "investor_take",
        "key_points",
        "rejected_at",
    }
    assert _fk_targets("out_of_scope_articles", "curation_id") == {
        "article_curations.id"
    }
    assert {
        "uq_out_of_scope_articles_curation_id",
        "ck_out_of_scope_articles_translated_title_not_empty",
        "ck_out_of_scope_articles_summary_not_empty",
        "ck_out_of_scope_articles_investor_take_not_empty",
    }.issubset(_constraint_names("out_of_scope_articles"))


def test_analyzed_article_fk_columns_use_explicit_id_name() -> None:
    watchlist_columns = set(WatchlistEntry.__table__.c.keys())
    watchlist_attrs = set(WatchlistEntry.__mapper__.attrs.keys())

    assert "analyzed_article_id" in watchlist_columns
    assert "analyzed_article_id" in watchlist_attrs
    assert "analysis_id" not in watchlist_columns
    assert "analysis_id" not in watchlist_attrs
    assert "article_analysis_id" not in watchlist_columns
    assert "assessment_id" not in watchlist_columns
    assert _fk_targets("watchlist_entries", "analyzed_article_id") == {
        "analyzed_articles.id"
    }

    exclusion_columns = set(EmbeddingBackfillExclusion.__table__.c.keys())
    exclusion_attrs = set(EmbeddingBackfillExclusion.__mapper__.attrs.keys())

    assert "analyzed_article_id" in exclusion_columns
    assert "analyzed_article_id" in exclusion_attrs
    assert "analysis_id" not in exclusion_columns
    assert "analysis_id" not in exclusion_attrs
    assert "article_analysis_id" not in exclusion_columns
    assert "assessment_id" not in exclusion_columns
    assert _fk_targets("embedding_backfill_exclusions", "analyzed_article_id") == {
        "analyzed_articles.id"
    }


def test_curation_relationships_use_article_state_names() -> None:
    relationships = set(ArticleCuration.__mapper__.relationships.keys())

    assert "analyzable_article" in relationships
    assert "analyzed_article" in relationships
    assert "out_of_scope_article" in relationships
    assert "in_scope_assessment" not in relationships
    assert "out_of_scope_assessment" not in relationships


def test_contract_migration_renames_stage4_article_tables() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "x5_analyzed_articles.py"
    )

    assert migration.exists()
    source = migration.read_text()
    assert 'MIGRATION_KIND = "contract"' in source
    assert 'op.rename_table("in_scope_assessments", "analyzed_articles")' in source
    assert (
        'op.rename_table("out_of_scope_assessments", "out_of_scope_articles")' in source
    )
    assert "drop_table" not in source
    assert "create_table" not in source
