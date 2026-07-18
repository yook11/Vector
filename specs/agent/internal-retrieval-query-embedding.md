# Internal Retrieval Query Embedding Spec

Status: Implemented
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
- query embedding cache は embedder 呼び出し前に lookup する。
- query embedding cache の hash 化は repository 内で行い、呼び出し側へ `query_hash` を露出しない。
- cache miss は正常系であり、lookup / save failure では内部検索 request を止めない。
- cache lookup / save の transaction は、記事検索などの主処理 transaction から分離する。

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

`InternalSearchService` は agent から見た内部検索境界の入口である。この仕様では `QuestionPlan` から query embedding を作る境界を定義する。query embedding を使った DB 記事検索と返却型は `internal-retrieval-article-search.md` で定義する。

- `retrieval_mode="internal"` / `"internal_and_external"` の場合だけ query embedding を実行する。
- `retrieval_mode="none"` / `"external"` の場合は embedder を呼ばない。
- 完成済み `QuestionPlan` では internal 系 mode の query は空にならない。防御的に正規化後 query が空の場合も embedder を呼ばない。
- embedder の戻り値は次段の article search boundary に渡せる形で返す。

## Query Embedding Cache

`InternalSearchService` は query を embedder に渡す前に cache lookup を行う。

```text
InternalSearchQueries
  -> cache lookup
  -> miss した query だけ embedder
  -> 新規 vector を cache save
  -> InternalQueryEmbedding
```

cache repository の public API は query text を受け取る。`query_hash` は repository の内部で導出し、service / embedder / article search repository は扱わない。

不変条件:

- hash する文字列と embed する文字列は同じである。
- repository は `query_text` から `query_hash` を導出する。
- 平文 query は cache table に保存しない。
- `embedder_identity` が異なる cache entry は別空間として扱う。
- repository は `commit()` しない。
- cache miss は「DB への確認は成功したが cached vector が存在しない」正常結果である。
- lookup failure は「DB 接続失敗、timeout、transaction error などで確認できない」異常である。

Failure policy:

- cache miss は failure として記録しない。必要なら hit/miss outcome metric として別に観測する。
- cache lookup failure は metric / log / audit で観測できるようにする。
- cache lookup failure 時は cache に存在しないとは断定せず、cache unavailable として扱う。
- cached vector が得られなかった query は embedder を呼ぶ。
- cache save failure でも、得られた `InternalQueryEmbedding` は返す。
- cache save failure は metric / log / audit で観測可能にする。
- cache failure だけで user-facing error にしない。

Transaction policy:

- cache lookup / save は主処理 transaction と分離する。
- cache 用 session / transaction は service helper または composition 層が用意する。
- cache transaction が失敗したら rollback して閉じる。
- article search 側 session は cache transaction の成否に依存しない。

## Gemini Query Embedder

`GeminiQueryEmbedder` は `InternalQueryEmbedder` の Gemini 実装である。

- model は `gemini-embedding-001`。
- `EmbedContentConfig.task_type` は `RETRIEVAL_QUERY`。
- `output_dimensionality` は `768`。
- `contents` には正規化済み query を順序通り渡す。
- 空の `InternalSearchQueries` では API を呼ばず `[]` を返す。
- response の embeddings 件数が query 件数と一致しない場合は provider response shape 違反として扱う。

## Non-goals

- DB 記事検索 repository の query / 返却型はこの仕様では固定しない。`internal-retrieval-article-search.md` で扱う。
- API endpoint / frontend response shape は変更しない。
- `QuestionPlan` に query 件数 validation は追加しない。件数 cap は internal retrieval 境界が担う。
- query embedding cache の DB schema は変更しない。

## Test Plan

- `build_internal_search_queries` が strip / blank drop / casefold dedupe / order preservation / cap 3 を行う。
- `InternalSearchQueries` が3件以下を許容し、4件以上と空 query を拒否する。
- `InternalQueryEmbedding` が空白 query を拒否し、`EmbeddingVector` を保持する。
- `InternalSearchService` が internal 系 mode だけ embedder を呼ぶ。
- `none` / `external` では embedder を呼ばない。
- `GeminiQueryEmbedder` が `RETRIEVAL_QUERY` と 768 次元で Gemini embedding API を呼ぶ。
- cache hit した query は embedder を呼ばない。
- cache miss は正常系であり、failure として記録されない。
- cache miss した query は embedder を呼ぶ。
- cache lookup failure は failure として観測されるが、cached vector が得られなかった query を embedder に渡して処理を続ける。
- cache save failure でも `InternalQueryEmbedding` は返る。
- cache repository が `query_text` から hash 化し、service 側が `query_hash` を扱わない。
- live smoke で実際に 768 次元の query vector が返ることを確認する。
