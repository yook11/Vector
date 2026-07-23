# Internal Retrieval Article Search Spec

Status: Implemented
Created: 2026-06-29
Scope: Query embedding を使って DB から内部の分析済み記事を取得する境界

## Problem

Planner が作った `article_search_queries` は完成済み `SearchPlan` で正規化され、`InternalSearchQueries` として query embedding に変換できる。次は、その vector を使って DB から近い内部記事を取得し、メイン agent が回答生成に使える記事内容へ変換する必要がある。

この作業では「近い記事をどう検索するか」と「検索結果をどの型で agent に渡すか」を固定する。API endpoint、回答生成、外部検索、UI 表示はまだ扱わない。

## Evidence

- 現在の article embedding は `AnalyzedArticleRecord.embedding` に保存されている。
- 既存の類似記事 API は `ArticleRepository.fetch_similar_to()` で `AnalyzedArticleRecord.embedding.cosine_distance(...)` を使っている。
- `AnalyzedArticleRecord` は Stage 4 assessment で in-scope と判定された記事の ORM である。
- `ArticleCuration` は assessment 前の curation 結果であり、out-of-scope に落ちる記事も含みうる。
- `UserVisibleArticle` という別名の専用型は作らない。現時点では `InScopeAnalyzedArticle` が、永続化前・DB 読み戻し時・agent / user に見せる分析済み記事の境界型である。
- Stage 4 category enum の重複解消方針は [`assessment-category-taxonomy.md`](../analysis/assessment-category-taxonomy.md) で定義する。内部検索の read path は `InScopeCategory` を使って `InScopeAnalyzedArticle` を復元する。
- 現在の public article API は `AnalyzedArticleRecord` を起点に `ArticleBrief` / `ArticleDetail` を構築している。
- `ArticleBrief` / `ArticleDetail` は UI/API 表示用 schema であり、agent の RAG 入力用 schema ではない。

## Decisions

- 近い記事を取得する DB query は ORM model ではなく repository に定義する。
- 検索対象は `AnalyzedArticleRecord` を起点にする。
- `ArticleCuration` だけを起点にした検索はしない。
- 内部検索で返してよい記事は、`InScopeAnalyzedArticle` として復元でき、かつ `AnalyzedArticleRecord.embedding IS NOT NULL` の記事だけとする。
- `InScopeAnalyzedArticle` は、永続化前・DB 読み戻し時・agent / user に見せる分析済み記事の境界型として扱う。
- `embedding IS NOT NULL` は `InScopeAnalyzedArticle` の field には含めず、内部検索 repository の検索条件として保証する。
- 内部検索では DB row を agent 用 projection へ直接詰め替えず、必ず `InScopeAnalyzedArticle.from_persisted_values()` を経由する。
- Agent 用には UI schema を再利用せず、内部検索専用の projection 型を作る。
- メイン agent / 回答生成に渡す主データは記事内容であり、検索メタデータは原則 prompt に出さない。
- 内部検索 hit の返却型は `article: InScopeAnalyzedArticle`, `content: InternalArticleContent`, `distance: float` に絞る。
- top-level の `article_id`, `source_name`, `category_name`, `matched_query` は v1 では持たない。
- `matched_query` は v1 では持たない。必要になったら audit/debug 用に別途追加する。
- 記事検索前の query embedding は cache を先に lookup し、miss した query だけ embedder を呼ぶ。
- cache miss は正常系、lookup failure / save failure は異常だが内部検索全体は止めない。
- query text の hash 化は cache repository の責務とし、service / embedder / article search repository へ `query_hash` を露出しない。
- cache lookup / save は主処理の DB transaction から分離する。

## Invariants

Internal article search repository は次を守る。

- 返す記事は必ず `AnalyzedArticleRecord` から構築する。
- `AnalyzedArticleRecord.embedding IS NOT NULL` の行だけ返す。
- 返す記事は必ず `InScopeAnalyzedArticle` として復元できる。
- `OutOfScopeArticleRecord` から構築した記事は返さない。
- 未 assessment の `ArticleCuration` は返さない。
- DB query は cosine distance 昇順で近い記事を返す。
- repository 内で `commit()` しない。
- `original_content` は読み戻さない。
- embedding vector は返却型に含めない。
- raw `key_points` JSON は agent にそのまま渡さない。
- raw DB row は agent に渡さない。
- top-level `article_id` は返さない。検索 hit の内部識別が必要な場合は `article.curation_id` を使う。
- agent 用 projection へ詰め替える前に `InScopeAnalyzedArticle.from_persisted_values()` で復元する。
- query embedding cache の lookup / save failure は記事検索の失敗として扱わない。

## Query Embedding Cache Policy

内部記事検索では、query text を vector 化する前に `query_embedding_cache` を lookup する。

cache は再計算を避けるための性能最適化であり、回答生成に必要な正本ではない。そのため、cache の読み書き失敗は内部検索 request 全体を失敗させない。

`cache miss` と `lookup failure` は分けて扱う。

- `cache miss`: DB への確認は成功したが、該当 query の cached vector が存在しなかった状態。正常系であり、failure として記録しない。
- `lookup failure`: DB 接続失敗、timeout、transaction error などにより、cache に存在するか確認できなかった状態。異常として観測するが、内部検索は続行する。

lookup / save の方針:

- cache hit の query は cached vector を使い、embedder を呼ばない。
- cache miss の query は正常系として embedder に渡す。
- lookup failure 時は cache に存在しないとは断定せず、cache unavailable として扱う。
- cached vector が得られなかった query は通常通り embedder に渡す。
- save failure でも、手元の `InternalQueryEmbedding` は破棄せず記事検索へ進む。
- lookup / save failure は metric と必要な warning log で観測できるようにする。

transaction の方針:

- cache repository は `commit()` しない。
- cache 用 session / transaction は service helper または composition 層で用意する。
- lookup / save は cache 専用 transaction で実行する。
- cache transaction が失敗したら rollback して閉じる。
- article search session は cache transaction の成否に依存しない。

## Visible Analyzed Article Boundary

現時点で、ユーザーまたは agent に見せてよい分析済み記事の境界型は `InScopeAnalyzedArticle` である。

内部検索で返してよい記事は次を満たす。

```text
InScopeAnalyzedArticle として復元できる
かつ
AnalyzedArticleRecord.embedding IS NOT NULL
```

`InScopeAnalyzedArticle` は次の境界を担う。

- assessment 結果を `analyzed_articles` に永続化する前の保存可能 snapshot。
- `analyzed_articles` から読み戻した値を再検証する domain boundary。
- agent / user に見せる分析済み記事として扱うための境界。

`embedding IS NOT NULL` は「内部ベクトル検索の対象になれる」条件であり、記事内容そのものではない。そのため `InScopeAnalyzedArticle` には含めず、repository の query 条件として保証する。

将来、公開停止、source 停止、品質フラグ、権限、削除状態などが入る場合は、`InScopeAnalyzedArticle` に追加で適用する visibility policy として repository contract に追加する。

## Return Shape Summary

内部検索が返す hit は次だけを持つ。

```python
class InternalArticleSearchHit(BaseModel):
    article: InScopeAnalyzedArticle
    content: InternalArticleContent
    distance: float
```

返す前提:

- `article` は `InScopeAnalyzedArticle` として復元済みである。
- 対応する `AnalyzedArticleRecord.embedding` は `NULL` ではない。

返さないもの:

- embedding vector
- raw DB row
- raw `key_points` JSON
- top-level `article_id`
- `source_name`
- `category_name`
- `matched_query`

`article_id` 相当の内部識別が必要な場合は、返却型に別 field を増やさず `article.curation_id` を使う。`source_name` / `category_name` / `matched_query` は記事内容ではないため v1 の回答生成入力には含めない。

## Types

### InternalArticleContent

`InternalArticleContent` はメイン agent が回答生成に使う記事内容である。検索距離や query などの検索メタデータは含めない。

```python
class InternalArticleContent(BaseModel):
    title: str
    summary: str
    key_points: list[str]
    mentions: list[str]
    published_at: datetime | None
```

Field の意味:

- `title`: `AnalyzedArticleRecord.translated_title`。embedding 入力から title は外したが、回答生成・引用表示の文脈としては使う。
- `summary`: `AnalyzedArticleRecord.summary`。
- `key_points`: `key_points[].content` の有効な文字列だけを順序通り取り出す。
- `mentions`: `key_points[].mentions[].surface` だけを取り出す。`type` は含めない。
- `published_at`: 元記事の公開日時。時系列判断や回答文の補助に使う。

`source_name`, `category_name`, `distance`, `matched_query` は `InternalArticleContent` には含めない。これらは記事内容そのものではなく、回答生成 LLM へ渡すとノイズになりうる。

`InternalArticleContent` は `InScopeAnalyzedArticle` から作る projection である。`key_points` は `article.assessment_result.key_points[].content`、`mentions` は `article.assessment_result.key_points[].mentions[].surface` から作る。

### InternalArticleSearchHit

`InternalArticleSearchHit` は repository / retrieval service 内部で使う検索 hit である。retrieval service が sort / dedupe / 検証を行うための最小 metadata を含むが、回答生成 prompt にそのまま出す型ではない。

```python
class InternalArticleSearchHit(BaseModel):
    article: InScopeAnalyzedArticle
    content: InternalArticleContent
    distance: float
```

Field の意味:

- `article`: DB row から復元した `InScopeAnalyzedArticle`。返してよい分析済み記事であることの境界。
- `content`: 回答生成で使う記事内容。`article` から作る projection。
- `distance`: query embedding と article embedding の cosine distance。小さいほど意味的に近い。sort / dedupe / 検証用の内部 metadata。

`distance` は LLM に読ませるための情報ではない。検索結果の整理と検証に使う内部値である。

`InternalArticleSearchHit` は top-level `article_id` を持たない。同一記事判定が必要な場合は `article.curation_id` を使う。

`InternalArticleSearchHit` は embedding vector を持たない。embedding は検索条件と distance 計算にだけ使う内部値であり、agent に渡す記事内容ではない。

`InternalArticleSearchHit` は raw DB row や raw `key_points` JSON を持たない。DB row の内容は `InScopeAnalyzedArticle` と `InternalArticleContent` に詰め替えてから返す。

## Repository Boundary

実装場所は `app/agent/internal_retrieval/article_search.py` とする。

```python
class ArticleVectorSearchRepository(Protocol):
    async def search_by_embedding(
        self,
        embedding: InternalQueryEmbedding,
        *,
        limit: int,
    ) -> list[InternalArticleSearchHit]:
        ...
```

`ArticleVectorSearchRepository` は `InternalSearchService` が依存する article vector search port である。

具象実装は `PgVectorArticleSearchRepository` とする。`PgVectorArticleSearchRepository` は `InternalQueryEmbedding.vector` を DB query に使い、`InternalArticleSearchHit` を返す。

Repository は次を満たす行だけを検索対象にする。

- `AnalyzedArticleRecord.embedding IS NOT NULL`
- `AnalyzedArticleRecord` の保存済み値から `InScopeAnalyzedArticle` として復元できる

Repository は row を取得した後、必ず `InScopeAnalyzedArticle.from_persisted_values()` を呼ぶ。復元できない row は domain invariant breach として扱い、raw row を agent に渡さない。

Query の基本形:

```python
distance = AnalyzedArticleRecord.embedding.cosine_distance(query_vector)

select(...)
  .select_from(AnalyzedArticleRecord)
  .join(ArticleCuration, ArticleCuration.id == AnalyzedArticleRecord.curation_id)
  .join(
      AnalyzableArticleRecord,
      AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
  )
  .where(AnalyzedArticleRecord.embedding.is_not(None))
  .order_by(distance.asc(), AnalyzableArticleRecord.published_at.desc().nulls_last())
  .limit(limit)
```

実装時は既存の `ArticleRepository.fetch_similar_to()` と同じ pgvector cosine distance の使い方を踏襲する。

## Key Points And Mentions

`key_points` は JSONB なので、読み戻し時に raw JSON をそのまま agent へ渡さない。

`key_points` の扱い:

- `NULL` は旧行互換として `[]` に正規化する。
- `[]` は空配列として扱う。
- list は `InScope` / `KeyPoint` domain 型で検証する。
- list 以外、または list 内要素が `KeyPoint` として不正な場合は domain invariant breach として扱う。
- `InternalArticleContent.key_points` は検証済み `KeyPoint.content` を順序通り取り出す。

`mentions` の扱い:

- `surface` だけ採用する。
- `type` は `InternalArticleContent` には入れない。
- `normalize_mention_surface` 相当で整形する。
- 空文字は除外する。
- `casefold()` key で dedupe する。
- 表示文字列は最初に出た casing を保持する。

## Service Boundary

`InternalSearchService` は次の流れを担当する。

1. Runner から正規化済み `InternalSearchQueries` を受け取る。
2. query embedding cache を lookup する。
3. cache miss または lookup failure により cached vector が得られなかった query だけ `InternalQueryEmbedder` で embedding を作る。
4. 新規 embedding は best-effort で cache に保存する。
5. cache hit と新規 embedding を合わせた `InternalQueryEmbedding` を作る。
6. `ArticleVectorSearchRepository` で query ごとに記事を検索する。
7. 複数 query で同じ `article.curation_id` が出た場合は、最小 `distance` の hit を採用する。
8. 最終件数を cap して `list[InternalArticleSearchHit]` を返す。

queryのstrip・重複除外・最大3件へのcapは`plan_from_draft()`が所有する。`target_time_window`は外部根拠の公開期間であり、このrepositoryのstrict date filterには使わない。

回答生成 prompt へ渡す段階では、`InternalArticleSearchHit.content` を使う。`distance` は source 管理ではなく、sort / dedupe / 検証用に保持する。

## Non-goals

- DB schema は変更しない。
- `UserVisibleArticle` という別名の専用型は今回作らない。現時点の visible analyzed article boundary は `InScopeAnalyzedArticle` とする。
- API endpoint / frontend response shape は変更しない。
- 回答生成 prompt は今回固定しない。
- 外部リサーチpipelineの検索・選別policyはこの仕様では扱わない。
- source active / category / permission などの新しい visibility policy は追加しない。
- distance threshold は今回決めない。まず top K を返し、回答生成・sufficiency 判定側で扱う。
- query embedding cache の DB schema は変更しない。
- cache failure を user-facing error として返さない。

## Test Plan

- repository が `AnalyzedArticleRecord` 起点で記事を返す。
- `embedding IS NULL` の analyzed article は返さない。
- curation 済みだが未 assessment の記事は返さない。
- out-of-scope article は返さない。
- cosine distance の昇順で返す。
- `limit` を守る。
- `original_content` を読み戻さない。
- `InternalArticleContent` が title / summary / key_points / mentions / published_at を持つ。
- DB row から `InScopeAnalyzedArticle` を復元してから `InternalArticleContent` を作る。
- `InScopeAnalyzedArticle` として復元できない row は返さない。
- returned hit は embedding vector を含まない。
- returned hit は raw DB row を含まない。
- returned hit は raw key_points JSON を含まない。
- key_points が `NULL` / `[]` の場合は空配列で構築できる。
- malformed key_points は validation error になる。
- mention は surface のみ採用し、type は含めない。
- duplicate mention は 1 回だけになる。
- cache hit した query は embedder を呼ばず、その vector で記事検索する。
- cache miss は正常系であり、failure として記録されない。
- cache lookup failure は failure として観測されるが、cached vector が得られなかった query を embedder に渡して処理を続ける。
- cache save failure でも、得られた vector で記事検索を続ける。
- cache repository は `query_text` を受け取り repository 内で hash 化する。
- service / embedder / article search repository は `query_hash` を扱わない。
- cache 操作は主 article search transaction と分離される。
- 複数 query の結果を service で dedupe し、同一 persisted article row は最小 distance の hit を残す。

## Done

- 内部検索の DB 読み戻しは repository に置く方針が明文化されている。
- 現時点の visible analyzed article boundary が `InScopeAnalyzedArticle` であることが明文化されている。
- 内部検索で返してよい記事の条件が `InScopeAnalyzedArticle` として復元でき、かつ `embedding IS NOT NULL` であることが明文化されている。
- メイン agent に渡す記事内容と、検索内部で使う hit metadata が分離されている。
- query embedding cache の lookup / save failure が best-effort として扱われることが明文化されている。
- cache transaction と article search transaction を分ける方針が明文化されている。
- 次の実装で守るべき repository / service / test の境界が明文化されている。
