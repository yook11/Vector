# articles 名前空間整理: `articles` -> `analyzable_articles`

**Status: IMPLEMENTED / PR1〜PR4 相当まで反映**

## Problem

現在の `articles` table / `Article` ORM は、名前だけ見ると「公開記事」または
一般的な記事全体を指しているように見える。

しかし実体は、collection BC の出口契約である `AnalyzableArticle` を永続化した
record であり、「分析工程に進める品質基準を満たした元記事」を表す。

このため、次の語彙が衝突している。

- `incomplete_articles`: 本文補完待ちの記事候補。
- `articles`: 分析可能な元記事。
- public `/articles`: ユーザーが読む公開記事。
- `article_id`: pipeline / audit / observability を横断する追跡 ID。

永続化された分析可能記事を `analyzable_articles` に寄せ、Domain VO / ORM record
/ DB table / public API / audit の名前空間を明確に分離する。

## Evidence

現状の実装では、`AnalyzableArticle` domain VO が
`AnalyzableArticleRepository.save()` 経由で `analyzable_articles` に INSERT される。

```text
Domain VO:
  app.collection.domain.analyzable_article.AnalyzableArticle

Current ORM:
  app.models.analyzable_article_record.AnalyzableArticleRecord

Current DB table:
  analyzable_articles
```

`incomplete_articles` は `ObservedArticle` を保持する補完待ち queue であり、
completion 成功時に `analyzable_articles` へ昇格する。

`pipeline_events.article_id` は現在 `analyzable_articles.id` を参照しているが、
監査・観測上は工程固有語彙ではなく、記事ライフサイクル追跡用の横断
correlation key として使われる。

## Target Vocabulary

最終形は以下にする。

```text
Domain VO:
  AnalyzableArticle

ORM model:
  AnalyzableArticleRecord

DB table:
  analyzable_articles
```

意味づけ:

- `IncompleteArticle`: 本文補完待ち。まだ analyzable ではない作業キュー。
- `AnalyzableArticle`: 分析工程へ進める domain VO。id は持たない。
- `AnalyzableArticleRecord`: `AnalyzableArticle` を永続化した ORM record。id を持つ。
- `analyzable_articles`: 分析可能な元記事の DB table。
- `article_id`: public API / audit / observability における横断的な記事追跡 ID。

## Invariants

- `AnalyzableArticle` domain VO は id を持たない。
- DB record は id を持つため、domain VO と区別して `AnalyzableArticleRecord` と呼ぶ。
- `incomplete_articles` は analyzable になる前の queue であり、`analyzable_articles`
  とは別物。
- public API の `article_id` は維持する。ユーザーにとって見える記事概念は一つなので、
  `/articles/{article_id}` を変えない。
- audit / observability の `article_id` は維持する。工程語彙に依存しない横断
  correlation key として扱う。
- `pipeline_events.article_id` の物理 FK target は `analyzable_articles.id` だが、
  column 名は変えない。
- `article_curations.analyzable_article_id` / `curation_noises.analyzable_article_id`
  は state table の物理 FK column として使う。
- DB 変更は Alembic migration 経由のみ。
- 認証・認可、公開 API response shape は変更しない。

## Non-goals

今回の仕様では以下は対象外にする。

- public API の `article_id` rename。
- `pipeline_events.article_id` rename。
- Logfire `article_stage` span attribute `article_id` rename。
- audit payload `target_article_id` rename。
- `in_scope_assessments` / `out_of_scope_assessments` rename。
- `ArticleRepository` / `ArticleBrief` / `ArticleDetail` など public read API 語彙の
  rename。

## PR Split

### PR1: ORM / Python 語彙 rename

最初に Python 側の record 名を整理する。

- ORM record は `app.models.analyzable_article_record.AnalyzableArticleRecord`。
- import 先は新 record module に統一する。
- `__tablename__ = "analyzable_articles"`。
- 書込側 repository は `AnalyzableArticleRepository`。

目的は domain VO と ORM record の区別を先に作ること。

### PR2: DB table rename

`articles` table を `analyzable_articles` に rename する contract migration。

対象:

- table rename: `articles` -> `analyzable_articles`
- `AnalyzableArticleRecord.__tablename__ = "analyzable_articles"`
- PostgreSQL の table rename で FK target は追従するが、constraint / index / sequence /
  grant / documentation 名は必要に応じて rename する。
- `pipeline_events.article_id` は column 名を維持し、FK target だけ新 table に揃える。
- migration gate / deploy 手順を更新する。

これは stop-the-world 前提の contract migration。

Deploy runbook:

1. backend API / worker / scheduler を停止する。
2. queue の in-flight job が残っていないことを確認する。
3. Alembic migration を `x2_analyzable_articles`、`x3_analyzable_article_fks` まで適用する。
4. 新 backend image を起動する。
5. acquisition / curation / assessment / `/api/v1/articles` の smoke test を実行する。
6. 問題なければ worker / scheduler を再開する。

rolling deploy は不可。旧 backend が `articles` table や
`article_curations.article_id` / `curation_noises.article_id` column を参照したまま
動くと DB interface と不整合になる。

### PR3: FK column rename

- `article_curations.article_id` -> `analyzable_article_id`
- `curation_noises.article_id` -> `analyzable_article_id`
- backfill / curation queue helper 内部の DB column 参照を新名へ更新

ただし public API と audit / observability の `article_id` は維持する。

### PR4: 周辺 repository 語彙整理

- 書込側 repository は `AnalyzableArticleRepository`
- `ArticleRepository` は public read repository なので維持可。
- `ArticleBrief` / `ArticleDetail` は public API schema なので維持。

## Audit / Observability Policy

`article_id` は維持する。

理由:

- audit の目的は「どの記事ライフサイクルに属する event か」を追跡すること。
- `analyzable_article_id` のような工程語彙に寄せると、curation / assessment /
  embedding を横断する追跡軸として読みにくくなる。
- public API と運用者視点では `article_id` が自然。

仕様として以下を固定する。

```text
pipeline_events.article_id は analyzable_articles.id を指す。
ただし名前は article_id のまま維持する。
これは工程固有 ID ではなく、記事ライフサイクル追跡用の横断 correlation key である。
```

`payload.target_article_id` も維持する。削除などで top-level FK が `NULL` になった場合に、
削除前の追跡 ID を残すため。

## Done

- `articles` の現在責務が `AnalyzableArticle` 永続化先だと明文化されている。
- target vocabulary が決まっている。
- Domain VO / ORM record / DB table の名前が分離されている。
- public API と audit / observability の `article_id` 維持方針が明記されている。
- DB table rename と FK column rename が別 PR として分離されている。
- contract migration の有無と stop-the-world 要件が明記されている。
- 最終的に内部 persistence/model 層で曖昧な `Article` model 名が残らない。
- allowlist として public API schema の `ArticleBrief` / `ArticleDetail` / `article_id`
  は残してよい。
