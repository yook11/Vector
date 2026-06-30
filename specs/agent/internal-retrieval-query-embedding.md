# Internal Retrieval Query Embedding Spec

Status: Draft
Created: 2026-06-29
Scope: Planner の `internal_queries` を内部検索用 embedding に変換する境界

## Problem

LLM adapter は `QuestionPlanDraft` を返すため、draft 段階では空白、重複、4件以上の query が返る可能性がある。`QuestionPlan.from_draft()` で空白は除去され、完成済み `QuestionPlan` は空 query を持たない。内部検索境界では重複除外と最大3件 cap を行い、request 全体の失敗にしない。

## Decisions

- `QuestionPlanDraft.internal_queries` は緩い `list[str]` のままにする。
- 完成済み `QuestionPlan.internal_queries` は空 query を持たない。
- `internal_retrieval` 境界では重複除外 / 最大3件 cap を行う。
- Planner には最大3件を指示するが、schema validation では `maxItems` を使わない。
- 正規化後の型は `InternalSearchQueries` とし、以降は3件以下・空 query なしとして扱う。
- `InternalQueryEmbedding` は `query` と `EmbeddingVector` のペアを表す。
- `InternalQueryEmbedder` は query を embedding に変換するだけで、記事検索・DB参照・回答生成はしない。
- Gemini 実装は `gemini-embedding-001` を `RETRIEVAL_QUERY` task type で呼び出す。
- query embedding は既存 article embedding と比較できるように 768 次元で取得する。

## Contract

```python
MAX_INTERNAL_QUERIES = 3

class InternalSearchQueries(BaseModel):
    queries: tuple[str, ...]

def build_internal_search_queries(raw_queries: list[str]) -> InternalSearchQueries:
    ...

class InternalQueryEmbedding(BaseModel):
    query: str
    vector: EmbeddingVector

class InternalQueryEmbedder(Protocol):
    async def embed_queries(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalQueryEmbedding]: ...

class GeminiQueryEmbedder:
    async def embed_queries(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalQueryEmbedding]: ...
```

`build_internal_search_queries` は completed plan の query を internal retrieval 用 value object へ縮める soft boundary である。4件以上返っても例外にせず、先頭から最大3件へ縮める。

`InternalSearchQueries` は正規化後 value object である。直接4件以上や空 query を持たせた場合は、自前実装のバグとして validation error にする。

## Normalization

順序は次の通り。

1. 各 query を `strip()` する。
2. 空文字になった query を除外する。
3. `casefold()` key で重複除外する。
4. 表示・embedding 用文字列は最初に出た casing を保持する。
5. 入力順を維持する。
6. 最大3件に cap する。

## Service Boundary

`InternalSearchService` は agent から見た内部検索境界の入口である。この slice では記事検索までは行わず、`QuestionPlan` から query embedding を作るところまで担当する。

- `retrieval_mode="internal"` / `"internal_and_external"` の場合だけ query embedding を実行する。
- `retrieval_mode="none"` / `"external"` の場合は embedder を呼ばない。
- 完成済み `QuestionPlan` では internal 系 mode の query は空にならない。防御的に正規化後 query が空の場合も embedder を呼ばない。
- embedder の戻り値は次段の article search boundary に渡せる形で返す。

## Gemini Query Embedder

`GeminiQueryEmbedder` は `InternalQueryEmbedder` の Gemini 実装である。

- model は `gemini-embedding-001`。
- `EmbedContentConfig.task_type` は `RETRIEVAL_QUERY`。
- `output_dimensionality` は `768`。
- `contents` には正規化済み query を順序通り渡す。
- 空の `InternalSearchQueries` では API を呼ばず `[]` を返す。
- response の embeddings 件数が query 件数と一致しない場合は provider response shape 違反として扱う。

## Non-goals

- 実DBを使う記事検索は実装しない。
- API endpoint / frontend response shape は変更しない。
- `QuestionPlan` に query 件数 validation は追加しない。件数 cap は internal retrieval 境界が担う。
- DB schema は変更しない。

## Test Plan

- `build_internal_search_queries` が strip / blank drop / casefold dedupe / order preservation / cap 3 を行う。
- `InternalSearchQueries` が3件以下を許容し、4件以上と空 query を拒否する。
- `InternalQueryEmbedding` が空白 query を拒否し、`EmbeddingVector` を保持する。
- `InternalSearchService` が internal 系 mode だけ embedder を呼ぶ。
- `none` / `external` では embedder を呼ばない。
- `GeminiQueryEmbedder` が `RETRIEVAL_QUERY` と 768 次元で Gemini embedding API を呼ぶ。
- live smoke で実際に 768 次元の query vector が返ることを確認する。
