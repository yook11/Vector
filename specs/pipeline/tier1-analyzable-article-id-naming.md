# Tier 1: A 空間 article_id の内部命名整理

**Status: Implemented (PR #802)**

## Problem

内部処理で `analyzable_articles.id`、つまり A 空間 ID を裸の `article_id` と呼んでいる
ため、public API の `article_id`、`pipeline_events.article_id`、Z 空間の
`analyzed_articles.id` と混同しやすい。特に Ready DTO / backfill / queue 周辺では同じ
`int` として A/Z の取り違えを型で検出できない。

## Vocabulary

```text
analyzable_article_id   : A 空間。analyzable_articles.id
analyzed_article_id     : Z 空間。analyzed_articles.id
curation_id             : article_curations.id
out_of_scope_article_id : out_of_scope_articles.id
article_id              : 境界語彙としてのみ残す
```

## Boundary Semantics

```text
public API article_id            : Z 空間。ユーザーが見る記事 ID
pipeline_events.article_id       : A 空間。監査 correlation key
Logfire article_stage.article_id : A 空間。observability correlation key
audit payload target_article_id  : A 空間。削除後も残す snapshot key
```

## Invariants

- 内部で A を持つ field / argument / local は `analyzable_article_id` と呼ぶ。
- 内部で Z を持つ field / argument / local は `analyzed_article_id` と呼ぶ。
- A と Z は直接 join / 比較しない。
- A/Z の対応経路は `analyzed_articles.curation_id -> article_curations.analyzable_article_id`。
- `pipeline_events.article_id` は rename しない。
- public API の `article_id` は rename しない。
- Logfire span attribute `article_id` は rename しない。
- DB schema / migration / public API response shape は Tier 1 では変更しない。

## Evidence

rename 対象の A 空間 `article_id`（現コードで実在確認済み）:

```text
analysis/curation/domain/ready.py:33    CurationReadyBuildFacts.article_id
analysis/curation/domain/ready.py:78    ReadyForCuration.article_id
analysis/curation/domain/ready.py:54    CurationReadyBuildBlockedError.article_id
analysis/assessment/domain/ready.py:33  AssessmentReadyBuildFacts.article_id
analysis/assessment/domain/ready.py:73  ReadyForAssessment.article_id
analysis/assessment/domain/ready.py:50  AssessmentReadyBuildBlockedError.article_id
analysis/embedding/domain/ready.py:31   EmbeddingReadyBuildFacts.article_id
analysis/embedding/domain/ready.py:63   ReadyForEmbedding.article_id (analyzed_article_id=Z と同居)
analysis/curation/repository.py:28,72,85,105,154   CurationRepository の A 空間引数群
collection/article_completion/repository.py:26     CompletionSucceeded.article_id
collection/article_completion/repository.py:201     ArticleCompletionService.execute() の A 戻り値 chain
collection/article_acquisition/service.py:76,79,83  acquisition service の persisted_ids / article_id local
queue/messages/curation.py              CurationTrigger.article_id (wire field)
queue/tasks/completion.py:142,146        completion task の article_id local
queue/helpers/backlog.py:34             BackfillTarget.article_id
queue/tasks/backfill.py:522             curation backfill enqueue の A 空間 article_id
analysis/curation/cli/re_curate_all.py / recuration_service.py  re-curation CLI 内部変数・summary・ログ
analysis/assessment/service.py:107      out-of-scope 分岐の局所変数 (実体 out_of_scope_articles.id)
```

対象外（id を保持しないため）:

```text
analysis/embedding/domain/ready.py:40   EmbeddingReadyBuildBlockedError (code のみ、A/Z いずれの id も持たない)
```

## Scope

```text
CurationReadyBuildFacts.article_id        -> analyzable_article_id
ReadyForCuration.article_id               -> analyzable_article_id
CurationReadyBuildBlockedError.article_id -> analyzable_article_id
AssessmentReadyBuildFacts.article_id        -> analyzable_article_id
ReadyForAssessment.article_id               -> analyzable_article_id
AssessmentReadyBuildBlockedError.article_id -> analyzable_article_id
EmbeddingReadyBuildFacts.article_id       -> analyzable_article_id
ReadyForEmbedding.article_id              -> analyzable_article_id
EmbeddingReadyBuildBlockedError           -> 現状 A ID を保持しないため対象外
CurationRepository の A 空間引数群          -> analyzable_article_id
CompletionSucceeded.article_id            -> analyzable_article_id
ArticleCompletionService.execute() の戻り値 doc / local chain を A として明示
acquisition service/task の persisted_ids / article_id local -> analyzable_article_id 系
CurationTrigger.article_id                -> analyzable_article_id
BackfillTarget.article_id                 -> analyzable_article_id
curation backfill 系 method/local の A 空間 article_id -> analyzable_article_id
re-curation CLI の内部変数・summary・ログ   -> analyzable_article_id
AssessmentService out-of-scope 分岐の局所変数 analyzed_article_id -> out_of_scope_article_id
```

## CurationTrigger Deployment Policy

- Tier 1 は curation queue drain + stop-the-world deploy 前提にする。
- `CurationTrigger.article_id -> analyzable_article_id` は alias なしで改名する。
- 旧 in-flight `CurationTrigger` / 旧 `ReadyForCuration` message は deploy 前に残さない。
- Rolling deploy 互換はこの PR の要件にしない。
- 既存 docstring の rolling deploy 互換説明は新方針に更新する。

## re-curation CLI Policy

- re-curation CLI は public API ではないため、A 空間 ID を `analyzable_article_id` と呼ぶ。
- `--id-from` / `--id-to` は互換のため当面維持してよい。
- help text では `analyzable_article_id >= M` のように意味を明記する。
- 新 option 追加や旧 option deprecated は後続判断にする。

## Boundary Conversion Policy

- stage-specific audit repository 内部では A を `analyzable_article_id` と呼ぶ。
- `PipelineEventRepository.append(...)` に渡す瞬間だけ `article_id=analyzable_article_id`
  に戻す。
- Logfire `set_article_id(...)` / span attr `article_id` は observability 境界として維持する。
- `BackfillTarget.target_id` は stage 多態なので維持する。

## Allowed article_id

allowlist（これ以外の内部 `article_id` は Tier 1 完了後に残さない）:

```text
public API schema / route / public read stack の article_id
pipeline_events.article_id / PipelineEvent.article_id
PipelineEventRepository.append(article_id=...)
Logfire article_stage span attribute / set_article_id / curation_stage_span(article_id=...)
audit payload target_article_id
legacy key rejection や境界名維持を検証する tests
```

## Non-goals

```text
DB schema 変更
Alembic migration
public API route / schema rename
pipeline_events.article_id rename
Logfire span attribute article_id rename
audit payload target_article_id rename
過去 pipeline event payload rewrite
rolling deploy 互換 alias の追加
```

## Done

- A 空間の裸 `article_id` が allowlist 外に残っていない。
- `ReadyForEmbedding` は `analyzed_article_id` と `analyzable_article_id` の名前で A/Z が
  判別できる。
- `CurationTrigger` の wire field は `analyzable_article_id` になっている。
- `BackfillTarget` は `target_id` と `analyzable_article_id` の役割が名前で判別できる。
- out-of-scope 保存戻り値の局所変数が `out_of_scope_article_id` になっている。
- `/check` で backend 検証を通す。
