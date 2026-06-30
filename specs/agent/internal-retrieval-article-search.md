# Internal Retrieval Article Search Spec

Status: Draft
Created: 2026-06-29
Scope: Query embedding を使って DB から内部の分析済み記事を取得する境界

## Problem

Planner が作った `internal_queries` は query embedding に変換できるようになった。次は、その vector を使って DB から近い内部記事を取得し、メイン agent が回答生成に使える記事内容へ変換する必要がある。

この作業では「近い記事をどう検索するか」と「検索結果をどの型で agent に渡すか」を固定する。API endpoint、回答生成、外部検索、UI 表示はまだ扱わない。

## Evidence

- 現在の article embedding は `AnalyzedArticleRecord.embedding` に保存されている。
- 既存の類似記事 API は `ArticleRepository.fetch_similar_to()` で `AnalyzedArticleRecord.embedding.cosine_distance(...)` を使っている。
- `AnalyzedArticleRecord` は Stage 4 assessment で in-scope と判定された記事の ORM である。
- `ArticleCuration` は assessment 前の curation 結果であり、out-of-scope に落ちる記事も含みうる。
- 現状、`UserVisibleArticle` 専用型は存在しない。Stage 4 の保存可能 snapshot として `InScopeAnalyzedArticle` を追加する方針は `in-scope-analyzed-article.md` で定義する。
- Stage 4 category enum の重複解消方針は `assessment-category-taxonomy.md` で定義する。内部検索の read path は `InScopeCategory` を使って `InScopeAnalyzedArticle` を復元する。
- 現在の public article API は `AnalyzedArticleRecord` を起点に `ArticleBrief` / `ArticleDetail` を構築している。
- `ArticleBrief` / `ArticleDetail` は UI/API 表示用 schema であり、agent の RAG 入力用 schema ではない。

## Decisions

- 近い記事を取得する DB query は ORM model ではなく repository に定義する。
- 検索対象は `AnalyzedArticleRecord` を起点にする。
- `ArticleCuration` だけを起点にした検索はしない。
- `embedding IS NOT NULL` の記事だけを検索対象にする。
- 現状の user-visible 判定は `AnalyzedArticleRecord` に存在することと同義に扱う。
- ただし概念として `in-scope` と `user-visible` は別である。将来 visibility policy が増えた場合は別途型や条件を追加する。
- 内部検索では DB row を agent 用 projection へ直接詰め替えず、`InScopeAnalyzedArticle` に復元してから使う。
- Agent 用には UI schema を再利用せず、内部検索専用の projection 型を作る。
- メイン agent / 回答生成に渡す主データは記事内容であり、検索メタデータは原則 prompt に出さない。
- `matched_query` は v1 では持たない。必要になったら audit/debug 用に別途追加する。

## Invariants

Internal article search repository は次を守る。

- 返す記事は必ず `AnalyzedArticleRecord` から構築する。
- `AnalyzedArticleRecord.embedding IS NOT NULL` の行だけ返す。
- `OutOfScopeArticleRecord` から構築した記事は返さない。
- 未 assessment の `ArticleCuration` は返さない。
- DB query は cosine distance 昇順で近い記事を返す。
- repository 内で `commit()` しない。
- `original_content` は読み戻さない。
- raw `key_points` JSON は agent にそのまま渡さない。
- agent 用 projection へ詰め替える前に `InScopeAnalyzedArticle` へ復元する。

## Current Visibility Model

現状の実装には「ユーザーに見せていい記事」を表す専用型はない。

現在は実装上、次のように扱われている。

```text
ユーザーに見せていい記事 ~= AnalyzedArticleRecord に存在する記事
```

これは `AnalyzedArticleRecord` が in-scope assessment の結果だからである。ただし、この同一視は現在の実装都合であり、概念としては分けて考える。

- `in-scope`: AI assessment が投資判断に関係すると判定したこと。
- `user-visible`: アプリとしてユーザーに表示してよいこと。

将来、公開停止、source 停止、品質フラグ、権限、削除状態などが入る場合は `user-visible` を別条件として repository contract に追加する。

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

`InternalArticleContent` は `InScopeAnalyzedArticle` から作る projection である。`key_points` は `article.in_scope.key_points[].content`、`mentions` は `article.in_scope.key_points[].mentions[].surface` から作る。

### InternalArticleSearchHit

`InternalArticleSearchHit` は repository / retrieval service 内部で使う検索 hit である。メイン agent が source 管理や dedupe を行うための bookkeeping を含むが、回答生成 prompt にそのまま出す型ではない。

```python
class InternalArticleSearchHit(BaseModel):
    article_id: int
    article: InScopeAnalyzedArticle
    content: InternalArticleContent
    distance: float
```

Field の意味:

- `article_id`: 同じ記事が複数 query で返った時の dedupe と、最終 `InternalArticleSource` 構築に使う。
- `article`: DB row から復元した in-scope analyzed article snapshot。
- `content`: 回答生成で使う記事内容。`article` から作る projection。
- `distance`: cosine distance。複数 hit の sort / dedupe で最小 distance を採用するために使う。

`distance` は LLM に読ませるための情報ではない。検索結果の整理と検証に使う内部値である。

## Repository Boundary

実装場所は `app/agent/internal_retrieval/article_search.py` とする。

```python
class InternalArticleSearchRepository:
    async def search_by_embedding(
        self,
        embedding: InternalQueryEmbedding,
        *,
        limit: int,
    ) -> list[InternalArticleSearchHit]:
        ...
```

Repository は `InternalQueryEmbedding.vector` を DB query に使い、`InternalArticleSearchHit` を返す。

Repository は row を取得した後、`AnalyzedArticleRecord` の保存済み値から `InScopeAnalyzedArticle` を復元する。復元できない row は domain invariant breach として扱い、raw row を agent に渡さない。

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

1. `QuestionPlan` から internal query を取り出す。
2. `InternalQueryEmbedder` で query embedding を作る。
3. `InternalArticleSearchRepository` で query ごとに記事を検索する。
4. 複数 query で同じ `article_id` が出た場合は、最小 `distance` の hit を採用する。
5. 最終件数を cap して `list[InternalArticleSearchHit]` を返す。

回答生成 prompt へ渡す段階では、`InternalArticleSearchHit.content` を使う。`article_id` と `distance` は source 管理、dedupe、検証用に保持する。

## Non-goals

- DB schema は変更しない。
- `UserVisibleArticle` 専用型は今回作らない。
- API endpoint / frontend response shape は変更しない。
- 回答生成 prompt は今回固定しない。
- 外部検索との統合は今回扱わない。
- source active / category / permission などの新しい visibility policy は追加しない。
- distance threshold は今回決めない。まず top K を返し、回答生成・sufficiency 判定側で扱う。

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
- key_points が `NULL` / `[]` の場合は空配列で構築できる。
- malformed key_points は validation error になる。
- mention は surface のみ採用し、type は含めない。
- duplicate mention は 1 回だけになる。
- 複数 query の結果を service で dedupe し、同一 `article_id` は最小 distance の hit を残す。

## Done

- 内部検索の DB 読み戻しは repository に置く方針が明文化されている。
- 現状の user-visible 判定が `AnalyzedArticleRecord` 起点であることが明文化されている。
- `in-scope` と `user-visible` が概念上は別であることが明文化されている。
- メイン agent に渡す記事内容と、検索内部で使う hit metadata が分離されている。
- 次の実装で守るべき repository / service / test の境界が明文化されている。
