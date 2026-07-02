# Internal Retrieval Structure Spec

Status: Draft
Created: 2026-07-02
Scope: internal retrieval の責務名と配置を読みやすくする小整理

## Problem

`agent/internal_retrieval` は query embedding、query embedding cache、article vector search、内部検索 orchestration を扱う。

現状は `InternalSearchService` が内部検索全体の orchestration を担っている一方で、service が依存する article search port の名前が `InternalArticleSearcher` になっており、将来の `InternalSearcher` 概念と衝突しやすい。

この作業では挙動を変えず、article vector search の port / repository の名前だけを責務に合わせて整理する。

## Decisions

- `InternalSearchService` は維持する。
- `InternalSearcher` という名前は、将来の orchestrator-facing port 用に温存する。
- `InternalQueryEmbeddingCache` protocol は名前も場所も維持する。
- `QueryEmbeddingCache` という protocol 名は使わない。ORM model `QueryEmbeddingCache` と衝突するため。
- `InternalArticleSearcher` protocol は `ArticleVectorSearchRepository` に改名する。
- 具象 `InternalArticleSearchRepository` は `PgVectorArticleSearchRepository` に改名する。
- 旧名 alias は作らない。まだ外部 composition root に配線されていないため、曖昧な名前を残さない。

## Responsibilities

- `InternalSearchService`: plan から internal query を取り出し、cache lookup、miss 分 embedding、article vector search、dedupe / sort / limit を行う具象 service。
- `InternalQueryEmbedder`: query text を vector に変換する AI API port。
- `InternalQueryEmbeddingCache`: query embedding cache の service 依存 port。
- `ArticleVectorSearchRepository`: query embedding を受け取り、内部記事 hit を返す article search port。
- `PgVectorArticleSearchRepository`: pgvector cosine distance で `AnalyzedArticleRecord` を検索する SQLAlchemy 実装。

## Non-goals

- DB schema は変更しない。
- API endpoint / frontend response shape は変更しない。
- `InternalSearchService` を `InternalSearcher` に改名しない。
- `service.py` を `searcher.py` に移動しない。
- `ports.py` は作らない。port が増えた時に再検討する。
- `embed_plan_queries()` の public/private は変更しない。

## Test Plan

- 既存の internal retrieval service test が同じ挙動で通る。
- article search repository integration test が新しい具象名で通る。
- ruff check / format check が通る。
- DB query の挙動が変わっていないことを `make test-integration` で確認する。
