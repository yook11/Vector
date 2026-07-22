# Internal Retrieval Query Embedding Spec

Status: Implemented
Created: 2026-06-29
Scope: Planner の `article_search_queries` を内部検索用 embedding に変換する境界

## Problem

LLM adapter は `QuestionPlanDraft` を返すため、draft 段階では空白、重複、4件以上の query が返る可能性がある。`plan_from_draft()` は strip、空要素除去、casefold 重複除外、最大3件への cap を行う。完成済み `SearchPlan` は1〜3件の正規化済み query を必ず持ち、Runner は同じ値を `InternalSearchQueries` に変換して内部検索へ渡す。

## Decisions

- `QuestionPlanDraft.article_search_queries` は緩い `list[str]` のままにする。
- 完成済み `SearchPlan.article_search_queries` は1〜3件で、空 queryと重複を持たない。
- strip / 空要素除去 / 重複除外 / 最大3件 cap は `plan_from_draft()` が所有する。
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
MAX_ARTICLE_SEARCH_QUERIES = 3

class InternalSearchQueries(BaseModel):
    queries: tuple[str, ...]

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

`InternalSearchQueries` は正規化後 value object である。Runner は完成済み `SearchPlan.article_search_queries` を順序どおりtupleへ変換する。直接4件以上や空 query を持たせた場合は、自前実装のバグとして validation error にする。

## Normalization

順序は次の通り。

1. 各 query を `strip()` する。
2. 空文字になった query を除外する。
3. `casefold()` key で重複除外する。
4. 表示・embedding 用文字列は最初に出た casing を保持する。
5. 入力順を維持する。
6. 最大3件に cap する。

## Service Boundary

`InternalSearchService` は agent から見た内部検索境界の入口である。この仕様では `InternalSearchQueries` から query embedding を作る境界を定義する。query embedding を使った DB 記事検索と返却型は `internal-retrieval-article-search.md` で定義する。

- `DirectAnswerPlan` では内部検索境界を呼ばない。
- `SearchPlan` では内部記事検索と外部リサーチを常に両方開始し、内部枝へ `InternalSearchQueries` を渡す。
- 完成済み `SearchPlan` の query は空にならない。`InternalSearchQueries` 自体は既存のno-op用途として空tupleを許容し、空の場合はembedderを呼ばない。
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
- cache lookup failure は metric / log で観測できるようにする。
- cache lookup failure 時は cache に存在しないとは断定せず、cache unavailable として扱う。
- cached vector が得られなかった query は embedder を呼ぶ。
- cache save failure でも、得られた `InternalQueryEmbedding` は返す。
- cache save failure は metric / log で観測可能にする。
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
- 内部検索へ `target_time_window` によるstrict date filterを追加しない。
- query embedding cache の DB schema は変更しない。

## Test Plan

- `plan_from_draft()` が article query の strip / blank drop / casefold dedupe / order preservation / cap 3 を行う。
- `InternalSearchQueries` が3件以下を許容し、4件以上と空 query を拒否する。
- `InternalQueryEmbedding` が空白 query を拒否し、`EmbeddingVector` を保持する。
- `SearchPlan` の内部枝が完成済みqueryを `InternalSearchQueries` として渡す。
- `DirectAnswerPlan` では内部検索・embedderを呼ばない。
- `GeminiQueryEmbedder` が `RETRIEVAL_QUERY` と 768 次元で Gemini embedding API を呼ぶ。
- cache hit した query は embedder を呼ばない。
- cache miss は正常系であり、failure として記録されない。
- cache miss した query は embedder を呼ぶ。
- cache lookup failure は failure として観測されるが、cached vector が得られなかった query を embedder に渡して処理を続ける。
- cache save failure でも `InternalQueryEmbedding` は返る。
- cache repository が `query_text` から hash 化し、service 側が `query_hash` を扱わない。
- live smoke で実際に 768 次元の query vector が返ることを確認する。
