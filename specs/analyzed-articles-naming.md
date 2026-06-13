# `in_scope_assessments` / `out_of_scope_assessments` naming spec

**Status: DRAFT**

## Problem

`in_scope_assessments` / `out_of_scope_assessments` は、Stage 4 の工程名である
`assessment` を永続化 table 名に含んでいる。

しかし current domain 上の実体は以下である。

- `in_scope_assessments`: Stage 4 の AI 分析が完了し、ユーザーに表示可能になった記事。
- `out_of_scope_assessments`: Stage 4 の AI 分析結果として表示対象外になった記事。

永続化契約では工程名ではなく、保存される記事状態を表す語彙へ寄せる。

## Evidence

Current implementation では Stage 4 assessment の保存先として以下の ORM / table が
使われている。

```text
ORM:
  app.models.in_scope_assessment.InScopeAssessment
  app.models.out_of_scope_assessment.OutOfScopeAssessment

DB table:
  in_scope_assessments
  out_of_scope_assessments
```

`InScopeAssessment` は public `/api/v1/articles` の一覧/詳細、watchlist、category、
briefing、trend discovery、embedding backlog から「表示可能な分析済み記事」として
参照されている。

`OutOfScopeAssessment` は Stage 4 の out-of-scope 判定結果として保存され、
同一 curation に対して in-scope 側と DB trigger で排他になる。

## Naming Policy

工程・処理の名前としての `assessment` は残してよい。

```text
AssessmentRepository
AssessmentService
ReadyForAssessment
assessment task
assessment audit stage
```

一方、DB table / ORM record / FK column / persisted JSON key のような永続化契約では、
工程語彙ではなく実体語彙を使う。

## Target Vocabulary

### In-scope side

```text
DB table:
  in_scope_assessments -> analyzed_articles

ORM model:
  InScopeAssessment -> AnalyzedArticleRecord

Relationship:
  in_scope_assessment -> analyzed_article
```

`analyzed_articles` は、Stage 4 の AI 分析が完了し、ユーザーに表示可能になった記事を
表す。

`embedding` は Stage 5 の enrichment であり、`embedding IS NULL` の
`analyzed_articles` 行は存在しうる。

つまり `analyzed_articles` は「embedding まで完了した記事」ではない。

### Out-of-scope side

```text
DB table:
  out_of_scope_assessments -> out_of_scope_articles

ORM model:
  OutOfScopeAssessment -> OutOfScopeArticleRecord

Relationship:
  out_of_scope_assessment -> out_of_scope_article
```

`out_of_scope_articles` は、Stage 4 の AI 分析結果として表示対象外になった記事を表す。

## ID Naming

`analyzed_articles.id` を指す internal / persisted key は `analyzed_article_id` に
寄せる。

変更対象:

```text
watchlist_entries.article_analysis_id
  -> watchlist_entries.analyzed_article_id

embedding_backfill_exclusions.analysis_id
  -> embedding_backfill_exclusions.analyzed_article_id

briefing persisted JSON key assessment_id
  -> analyzed_article_id

embedding task/message/repository analysis_id
  -> analyzed_article_id
```

`analysis_id` は現状、実体として `in_scope_assessments.id` を指している。table を
`analyzed_articles` に rename するなら、queue message / repository 引数 / backlog
helper でも `analyzed_article_id` と呼ぶ方が永続化対象との対応が明確になる。

## Public Interface Invariants

以下は維持する。

```text
public API の article_id
audit / observability の article_id
Logfire / pipeline correlation key としての article_id
/api/v1/articles/{article_id}
ArticleBrief / ArticleDetail など public schema 名
```

理由:

- ユーザー視点では、読める記事の ID は `article_id` である。
- audit / observability の `article_id` は記事ライフサイクル横断の correlation key
  であり、特定工程や table 名に依存させない。

## PR Split

### PR1: table / ORM / relationship rename

目的: `assessment` table を記事状態ベースの名前へ変更する。

変更:

```text
in_scope_assessments -> analyzed_articles
out_of_scope_assessments -> out_of_scope_articles

InScopeAssessment -> AnalyzedArticleRecord
OutOfScopeAssessment -> OutOfScopeArticleRecord

in_scope_assessment -> analyzed_article
out_of_scope_assessment -> out_of_scope_article
```

含めるもの:

- SQLAlchemy model rename
- import / type reference rename
- relationship rename
- FK target rename
- constraint / index / sequence / pkey 名 rename
- Stage 4 排他 trigger / function の table 名更新
- repository / query / test の追従

維持するもの:

- public API `article_id`
- audit / Logfire `article_id`
- `AssessmentRepository` / `AssessmentService` など工程名
- `analysis_id` / `assessment_id` / `article_analysis_id` 系の internal ID cleanup

### PR2: internal ID contract rename

目的: `analyzed_articles.id` を指す internal key 名を `analyzed_article_id` に揃える。

変更:

```text
watchlist_entries.article_analysis_id -> analyzed_article_id
embedding_backfill_exclusions.analysis_id -> analyzed_article_id
briefing JSON assessment_id -> analyzed_article_id
embedding task/message/repository analysis_id -> analyzed_article_id
```

含めるもの:

- DB column rename
- FK / index / constraint 名 rename
- ORM field rename
- queue message field rename
- backlog / embedding task / embedding repository の引数名 rename
- briefing persisted JSON の key migration
- tests 更新

注意:

- queue に旧 `analysis_id` message が残っていると新 worker で読めない可能性がある。
- PR2 deploy 前に embedding queue を drain するか、一時的な互換読み取りを持つかを
  実装前に決める。

## Migration Policy

PR1 / PR2 は DB 契約変更を含むため contract migration とする。

- drop / recreate ではなく rename を使う。
- 既存データは保持する。
- downgrade を実装する。
- stop-the-world deploy 前提にする。
- old backend が new schema を読む状態は避ける。

## Invariants

- `analyzed_articles` は Stage 4 完了済みで表示可能な記事を表す。
- `embedding` は nullable のまま維持する。
- `out_of_scope_articles` は表示対象外になった記事を表す。
- in-scope / out-of-scope は同一 curation に対して排他である。
- public API / audit / observability の `article_id` は維持する。
- `assessment` は工程名としてのみ残る。
- DB 変更は Alembic migration 経由のみ行う。

## Non-goals

今回やらないこと:

- public API の `article_id` rename。
- audit / Logfire の `article_id` rename。
- `pipeline_events.article_id` rename。
- Stage 4 の工程名 `Assessment` の rename。
- Stage 4 の domain result `InScope` / `OutOfScope` の意味変更。
- embedding 完了済みだけを表示可能記事とみなす仕様変更。

## Acceptance Criteria

PR1 後:

```text
current app の InScopeAssessment / OutOfScopeAssessment 参照が 0。
current model / FK の in_scope_assessments / out_of_scope_assessments 参照が 0。
AnalyzedArticleRecord / OutOfScopeArticleRecord が current ORM record になる。
public API の article_id は維持される。
audit / observability の article_id は維持される。
```

PR2 後:

```text
analyzed_articles.id を指す article_analysis_id / analysis_id / assessment_id が残らない。
watchlist_entries.analyzed_article_id が analyzed_articles.id を参照する。
embedding_backfill_exclusions.analyzed_article_id が analyzed_articles.id を参照する。
briefing JSON key が analyzed_article_id になる。
embedding task/message/repository が analyzed_article_id を使う。
```

## Test Plan

PR1 / PR2 共通:

```bash
cd backend
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/ -m unit -x -q
make test-integration
```

Migration verification:

```bash
cd backend
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
uv run alembic check
```

Focused regression:

- `/api/v1/articles` 一覧/詳細。
- watchlist add/remove/list。
- category filter。
- Stage 4 assessment save in-scope / out-of-scope。
- Stage 5 embedding ready / save / backlog。
- briefing key article persistence / readback。
- trend discovery key point reads。
- Stage 4 排他 trigger。
- DB user isolation grants。

## Done

- `assessment` を永続化 table / key 名から外す方針が明文化されている。
- `analyzed_articles` / `out_of_scope_articles` の意味が明文化されている。
- `analyzed_article_id` へ寄せる internal ID 方針が明文化されている。
- public API / audit / observability の `article_id` 維持方針が明文化されている。
- PR1 と PR2 の責務境界が明文化されている。
- contract migration と stop-the-world deploy の必要性が明文化されている。
