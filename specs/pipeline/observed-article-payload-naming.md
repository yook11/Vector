# `staged_attributes` -> `observed_article` naming spec

Status: Implemented (PR #799)

## Summary

`incomplete_articles.staged_attributes` は、Stage 1 で観測できた記事事実
`ObservedArticle` の JSONB payload を保持している。

`staged_attributes` は「一時的に stage された属性」という実装目線の名前で、
現在の domain 語彙 `ObservedArticle` とずれている。DB column / ORM field /
completion DTO / tests / current docs の語彙を `observed_article` に揃える。

`incomplete_articles` table 名は今回は変更しない。この table は
`ObservedArticle` を analyzable に昇格させるための state / lease table として
扱う。

## Naming Contract

最終形は以下に揃える。

```text
Domain VO:
  ObservedArticle

DB table:
  incomplete_articles

DB column:
  observed_article

ORM field:
  IncompleteArticle.observed_article

Ready DTO:
  ArticleCompletionReadyBuildFacts.observed_article

Domain constructor:
  ObservedArticle.try_build(...)
```

`ObservedArticle.try_build(...)` は、永続化された `observed_article` payload と
DB 表層列の authoritative identity から `ObservedArticle` を構築する。

```python
ObservedArticle.try_build(
    observed_article=facts.observed_article,
    source_name=facts.source_name,
    source_url=source_url,
)
```

この箇所の `try_build` は `None` を返さない。復元不能な場合は既存通り
`ObservedArticleInvalidError` を投げ、completion audit が reason を記録できる
ようにする。`try` は「通常 constructor より境界に近い生 payload から構築を
試みる」という意味で使う。

## Behavior

Stage 1 で `ObservedArticle` が生成された場合、
`IncompleteArticleRepository.save()` は `observed_article` column に JSONB payload
を保存する。

保存時は `ObservedArticle.to_staged_attributes()` のような永続化 helper を
残さず、repository 側で明示的に dump する。

```python
observed_article=observed.model_dump(mode="json", by_alias=True)
```

Stage 2 で completion ready を構築する場合、repository は `observed_article`
column を読み、`ArticleCompletionReadyBuildFacts.observed_article` として渡す。
`ReadyForArticleCompletion.try_advance_from()` は `ObservedArticle.try_build(...)`
を呼んで VO を復元する。

## API / Observability

Public API の request / response shape は変更しない。

Audit / Logfire の stage / event / `article_id` 語彙は変更しない。

`ObservedArticleInvalidReason` は以下に rename する。

```text
STAGED_ATTRIBUTES_NOT_OBJECT
-> OBSERVED_ARTICLE_NOT_OBJECT
```

wire value も新語彙へ変更する。

```text
"staged_attributes_not_object"
-> "observed_article_not_object"
```

これは internal completion failure classification の語彙更新として扱う。過去ログの
値は移行しない。

## Migration

新しい Alembic migration を current head の後に追加する。

```text
incomplete_articles.staged_attributes
-> incomplete_articles.observed_article
```

rename は `op.alter_column(..., new_column_name="observed_article")` または
PostgreSQL の `ALTER TABLE ... RENAME COLUMN ...` を使い、drop / recreate は
しない。既存 JSONB データはそのまま保持する。

downgrade では逆方向に rename する。

この変更は DB column rename のため contract migration とする。旧 backend は
`staged_attributes` を参照し、新 backend は `observed_article` を参照するため、
deploy は stop-the-world 前提にする。

## Code Changes

主な変更対象:

```text
backend/app/models/incomplete_article.py
backend/app/collection/article_acquisition/repository.py
backend/app/collection/article_completion/repository.py
backend/app/collection/article_completion/ready.py
backend/app/collection/domain/observed_article.py
```

主な変更:

- `IncompleteArticle.staged_attributes` を `observed_article` に rename する。
- `ArticleCompletionReadyBuildFacts.staged_attributes` を `observed_article` に
  rename する。
- `ObservedArticle.to_staged_attributes()` を削除する。
- `ObservedArticle.from_staged_attributes(...)` を `ObservedArticle.try_build(...)`
  に置換する。
- `ObservedArticleInvalidReason.STAGED_ATTRIBUTES_NOT_OBJECT` を
  `OBSERVED_ARTICLE_NOT_OBJECT` に rename し、wire value も
  `observed_article_not_object` にする。
- tests の direct SQL / assertions / helper 名を `observed_article` に更新する。
- current docs / specs の現行契約だけ更新し、歴史的 migration の過去説明は
  原則残す。

## Test Plan

最低限実行する。

```bash
cd backend
uv run ruff check app/
uv run ruff format --check app/
uv run pytest tests/ -m unit -x -q
make test-integration
```

migration 検証:

```bash
cd backend
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
uv run alembic check
```

重点確認:

- Stage 1 acquisition が `incomplete_articles.observed_article` に JSONB を保存する。
- Stage 2 completion が `observed_article` から `ObservedArticle` を復元できる。
- invalid JSONB が `OBSERVED_ARTICLE_NOT_OBJECT` reason で分類される。
- direct SQL tests が新 column 名を使う。
- `rg "staged_attributes|to_staged_attributes|from_staged_attributes|STAGED_ATTRIBUTES"`
  が current app / tests で 0 になる。ただし歴史的 migration / specs は allowlist。

## Local Apply

実装後、ローカル DB には migration を適用する。

```bash
cd backend
uv run alembic upgrade head
```

アプリを動かしている場合は、backend / worker / scheduler を止めてから migration
を適用し、新コードで再起動する。旧コードと新 DB、新コードと旧 DB の組み合わせは
どちらも壊れるため、rolling deploy は避ける。
