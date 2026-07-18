# Tier 3 - Curation noise columns and completion incomplete article naming

**Status: Implemented (PR #812)**

Tier 3 は pipeline stage naming roadmap の `curation-01` と `completion-01`
を扱う。

主目的は次の 2 つ。

1. Stage 3 curation の signal / noise 永続表で、同じ翻訳タイトル・要約を
   `translated_title` / `summary` として揃える。
2. Stage 2 completion で `incomplete_articles.id` を指すアプリ層の
   `pending_*` 語彙を `incomplete_article_*` に揃える。

## Problem

### Curation

Stage 3 curation は signal / noise を排他 2 表で保存するが、同じ概念の列名が
表ごとに分かれている。

```text
article_curations.translated_title / summary
curation_noises.title_ja / summary_ja
```

このため、signal / noise を横断して「curation が生成した翻訳タイトル・要約」
として扱うとき、同じ意味なのに列名を反転させる必要がある。

AI 境界では `title_ja` / `summary_ja` が prompt / SDK contract として妥当だが、
永続化された curation 結果の表現としては signal 側と noise 側で語彙が揃って
いない。

### Completion

Stage 2 completion の永続層は `IncompleteArticle` / `incomplete_articles`
に揃っている。一方、アプリ層では同じ行 ID を `pending_id` と呼んでいる。

```text
incomplete_articles.id
ReadyForArticleCompletion.pending_id
CompletionPayload.pending_id
CompletionPayload.pending_status
scrape_html_body(pending_id)
```

`pending_id` は実体として `incomplete_articles.id` だが、`pending` は他 stage
の「未処理」集合にも使われる一般語であり、複数の int ID 空間と混ざりやすい。

## Evidence

### Curation

- `backend/app/models/article_curation.py`
  - `ArticleCuration.translated_title`
  - `ArticleCuration.summary`
  - CHECK constraint:
    - `ck_article_curations_translated_title_not_empty`
    - `ck_article_curations_summary_not_empty`
- `backend/app/models/curation_noise.py`
  - `CurationNoise.title_ja`
  - `CurationNoise.summary_ja`
  - CHECK constraint:
    - `ck_curation_noises_title_ja_not_empty`
    - `ck_curation_noises_summary_ja_not_empty`
- `backend/app/analysis/curation/repository.py`
  - signal path は `Signal.title_ja` / `summary_ja` を
    `ArticleCuration.translated_title` / `summary` に写す。
  - noise path は `Noise.title_ja` / `summary_ja` を
    `CurationNoise.title_ja` / `summary_ja` に写す。
- `backend/app/analysis/curation/ai/schema.py`
  - AI SDK contract は `{relevance, title_ja, summary_ja}`。
- `backend/app/analysis/curation/domain/result.py`
  - Domain result も `Signal.title_ja` / `summary_ja`、
    `Noise.title_ja` / `summary_ja`。
- `backend/app/audit/stages/curation.py`
  - curation audit payload は title / summary を焼いていないため、
    `curation_noises` column rename は payload schema に直接影響しない。
- `backend/alembic/versions/t2_curation_table_rename.py`
  - 既存 migration で curation table rename は stop-the-world 前提。

### Completion

- `backend/app/models/incomplete_article.py`
  - ORM / DB table は `IncompleteArticle` / `incomplete_articles`。
  - `id` が Stage 2 completion の work item identity。
- `backend/app/collection/article_completion/ready.py`
  - `ArticleCompletionReadyBuildFacts.pending_id`
  - `ReadyForArticleCompletion.pending_id`
  - `ArticleCompletionReadyBuildPendingMissingError`
  - `ArticleCompletionReadyBuildPendingNotRunningError`
- `backend/app/collection/article_completion/repository.py`
  - `load_ready_build_facts(pending_id)`
  - `claim_ready_batch()` が `pending_ids` を返す。
  - `close_claimed` / `schedule_retry` / `_delete_claimed` は
    `ready.pending_id + ready.attempt_count` で attempt fence を張る。
- `backend/app/queue/tasks/completion.py`
  - `scrape_html_body(pending_id)` が taskiq input。
  - dispatch は `scrape_html_body.kiq(pending_id)` を positional に enqueue する。
- `backend/app/audit/domain/payloads.py`
  - `CompletionPayload.pending_id`
  - `CompletionPayload.pending_status`
- `backend/app/audit/stages/completion.py`
  - Ready build error audit だけが `pending_id` / `pending_status` を payload に焼く。
  - 通常の success / scrape failure / persist outcome は
    `canonical_url` / `attempt_count` 中心で、`pending_id` は焼かない。
- `backend/app/audit/repository.py`
  - `payload.model_dump(mode="json", exclude_none=False)` を JSONB に保存する。
- `backend/app/audit/domain/payloads.py`
  - `BasePipelineEventPayload.model_config` は `extra="ignore"`。
  - 旧 payload key を持つ JSONB は Pydantic validation では落ちないが、
    explicit field として残さない限り旧 key の値は hydrate されない。
- `backend/fly.core.toml` / `backend/fly.collect.toml`
  - production は core / collect の 2 Fly app。
  - completion worker は collect 側 `worker-fetch` の metadata / content workers。
  - curation worker は core 側 `worker-analysis`。

## Decisions

### Curation

1. curation 永続結果の canonical field name は `translated_title` / `summary` とする。

2. `curation_noises.title_ja` を `curation_noises.translated_title` に rename する。

3. `curation_noises.summary_ja` を `curation_noises.summary` に rename する。

4. CHECK constraint 名も column 名に合わせて rename する。

   ```text
   ck_curation_noises_title_ja_not_empty
   -> ck_curation_noises_translated_title_not_empty

   ck_curation_noises_summary_ja_not_empty
   -> ck_curation_noises_summary_not_empty
   ```

5. AI / prompt / SDK 境界の `title_ja` / `summary_ja` は rename しない。
   - `GEMINI_CURATION_SPEC` / AI response schema / parse / domain result は
     external AI contract として現状維持。
   - repository が AI/domain result を persistence field に写す。

6. Data rewrite はしない。DB column rename のみで既存値を保持する。

7. Deploy は stop-the-world とする。
   - rolling deploy は旧 code が rename 後 column を参照できない可能性がある。
   - expand/contract は今回の単純化目的に対して重い。

### Completion

8. `incomplete_articles.id` を指すアプリ層の canonical name は
   `incomplete_article_id` とする。

9. `pending_status` は `incomplete_article_status` に rename する。

10. New `CompletionPayload` は次の field を使う。

    ```text
    incomplete_article_id
    incomplete_article_status
    ```

11. Historical `pipeline_events.payload.pending_id` /
    `pipeline_events.payload.pending_status` は rewrite しない。

12. `CompletionPayload` の current schema からは `pending_id` /
    `pending_status` を外す。
    - 旧 payload は `extra="ignore"` により validation では落ちない。
    - 旧 key の値を current model field として hydrate する互換 alias は今は追加しない。
    - 将来 historical audit reader / dashboard が必要になった場合は、reader 側で
      `COALESCE(payload->>'incomplete_article_id', payload->>'pending_id')`
      相当を実装する。

13. New completion ready-build outcome code も `incomplete_article` 語彙へ寄せる。

    ```text
    completion_ready_build_blocked_pending_missing
    -> completion_ready_build_blocked_incomplete_article_missing

    completion_ready_build_blocked_pending_not_running
    -> completion_ready_build_blocked_incomplete_article_not_running
    ```

    Historical outcome_code は rewrite しない。横断集計が必要な reader / SQL は
    旧新両方を見る。

14. Ready-build typed error class name も `incomplete_article` 語彙へ寄せる。

    ```text
    ArticleCompletionReadyBuildPendingMissingError
    -> ArticleCompletionReadyBuildIncompleteArticleMissingError

    ArticleCompletionReadyBuildPendingNotRunningError
    -> ArticleCompletionReadyBuildIncompleteArticleNotRunningError
    ```

    これらの `Pending` は `pending_id` / `pending_status` そのものではないが、
    error は `incomplete_articles` 行の存在・状態を説明するため、class 名も
    current vocabulary に揃える。

15. task name `scrape_html_body` は rename しない。
    - タスクの責務は HTML body scrape であり、`pending` 語彙ではない。

16. `scrape_html_body` input parameter は `incomplete_article_id` に rename する。
    - 現状 enqueue は positional (`scrape_html_body.kiq(pending_id)`) なので
      wire key rename の影響は小さい。
    - ただし stop-the-world / queue drain 前提で進め、旧 message の混在を避ける。

17. DB table / ORM class の `IncompleteArticle` / `incomplete_articles` は rename しない。
    - 永続層は既に canonical name に揃っている。

18. `incomplete_articles.url` vs `source_url` は今回扱わない。
    - これは roadmap Tier 4 `acquisition-07`。
    - 本 Tier に混ぜると DB column rename がもう 1 つ増え、scope が膨らむ。

## Invariants

- Signal curation と noise curation の永続結果は、同じ概念を同じ field name で持つ。
- `article_curations.translated_title` と `curation_noises.translated_title` は
  どちらも Stage 3 curation が生成した翻訳タイトルである。
- `article_curations.summary` と `curation_noises.summary` はどちらも Stage 3
  curation が生成した事実ベース要約である。
- AI response / AI domain result の `title_ja` / `summary_ja` は external AI
  contract であり、persistence canonical name とは境界で写す。
- `incomplete_article_id` は常に `incomplete_articles.id` を指す。
- `incomplete_article_status` は常に `incomplete_articles.status` の snapshot である。
- New completion audit payload は `pending_id` / `pending_status` を出さない。
- Historical completion audit payload は rewrite しない。
- Completion queue deploy は旧 task message と新 task function が混在しない状態で行う。
- `attempt_count` は rename しない。completion の claim / retry 制御 snapshot として
  現状維持する。

## Non-goals

- Curation AI prompt / SDK response schema の rename。
- `Signal.title_ja` / `Signal.summary_ja` / `Noise.title_ja` /
  `Noise.summary_ja` の rename。
- `article_curations.translated_title` / `article_curations.summary` の rename。
- 過去 `pipeline_events.payload` の rewrite。
- Historical completion outcome_code の rewrite。
- Historical Alembic migration file の rewrite。
- `IncompleteArticle` ORM class / `incomplete_articles` table の rename。
- `incomplete_articles.url` -> `source_url` rename。
- Public API / frontend schema の変更。
- Task name `scrape_html_body` の rename。
- `pending` という一般語の全面禁止。
  - backlog / maintenance の「未処理集合」としての pending は scope 外。
  - 本 spec が禁止するのは `incomplete_articles.id/status` を指す
    `pending_id` / `pending_status` と、それに紐づく ready-build typed error
    class の `Pending` 語彙。

## Implementation Scope

### Curation files

- `backend/app/models/curation_noise.py`
  - field / CHECK constraint を `translated_title` / `summary` に変更。
- `backend/app/analysis/curation/repository.py`
  - `save_noise` が `translated_title=noise.title_ja`、
    `summary=noise.summary_ja` を書く。
- Alembic migration
  - `curation_noises.title_ja` -> `translated_title`
  - `curation_noises.summary_ja` -> `summary`
  - CHECK constraint rename。
  - downgrade は逆 rename。
- Tests
  - `backend/tests/analysis/curation/repository/test_curation_repository.py`
  - `backend/tests/test_ai_analyzer.py`
  - `backend/tests/test_maintenance_tasks.py`
  - `backend/tests/test_maintenance_backlog.py`

### Completion files

- `backend/app/collection/article_completion/ready.py`
  - facts / ready / errors / docstrings を `incomplete_article_*` に変更。
  - `ArticleCompletionReadyBuildPendingMissingError` を
    `ArticleCompletionReadyBuildIncompleteArticleMissingError` に変更。
  - `ArticleCompletionReadyBuildPendingNotRunningError` を
    `ArticleCompletionReadyBuildIncompleteArticleNotRunningError` に変更。
- `backend/app/collection/article_completion/repository.py`
  - method args / return locals / attempt fence を `incomplete_article_id` に変更。
- `backend/app/collection/article_completion/service.py`
  - logs を `incomplete_article_id` に変更。
- `backend/app/collection/article_completion/failure_handling.py`
  - logs を `incomplete_article_id` に変更。
- `backend/app/queue/tasks/completion.py`
  - dispatch locals / task arg / result dict / ready-build audit call を
    `incomplete_article_id` に変更。
- `backend/app/audit/domain/payloads.py`
  - `CompletionPayload.incomplete_article_id`
  - `CompletionPayload.incomplete_article_status`
  - remove current `pending_id` / `pending_status` fields.
- `backend/app/audit/stages/completion.py`
  - ready-build audit payload / function args / outcome code を rename。
- Tests
  - `backend/tests/test_audit_payloads.py`
  - `backend/tests/collection/article_completion/test_completer.py`
  - `backend/tests/collection/article_completion/test_ready.py`
  - `backend/tests/collection/article_completion/test_repository.py`
  - `backend/tests/collection/article_completion/test_dispatch.py`
  - `backend/tests/collection/test_scrape_html_body.py`
  - `backend/tests/collection/article_completion/test_article_completion_audit_repository.py`
  - `backend/tests/collection/article_completion/test_article_completion_failure_handler.py`
  - `backend/tests/collection/article_completion/test_service.py`
  - `backend/tests/collection/article_acquisition/test_repository.py`

## Deployment Runbook

This Tier includes a DB column rename and task input rename, so production deploy is
stop-the-world.

1. Stop scheduler first so no new cron jobs enqueue.
   - `your-vector-core-app` process: `scheduler`

2. Wait for or drain relevant Redis queues / in-flight tasks.
   - curation / assessment / embedding / maintenance side:
     - `pipeline:analysis`
     - `pipeline:embedding`
     - `pipeline:maintenance`
   - completion side:
     - `pipeline:metadata`
     - `pipeline:content`

3. Stop backend workers and API that share the DB schema.
   - `your-vector-core-app`
     - `api`
     - `worker-analysis`
     - `worker-insights`
     - `scheduler`
   - `your-vector-collect-app`
     - `worker-fetch`

4. Run Alembic migration from the approved owner DB path.
   - Fly `release_command` is not used in current backend Fly config.

5. Deploy the new image to both Fly apps.
   - `your-vector-core-app`
   - `your-vector-collect-app`

6. Resume machines / process groups.

7. Smoke test.
   - API health.
   - curation signal and noise path can write.
   - completion ready-build skipped/failed path writes
     `incomplete_article_id` / `incomplete_article_status`.
   - dispatch can claim `incomplete_articles` and enqueue `scrape_html_body`.

## RED-first Tests

Add / update the contract tests before the implementation change where they provide a
real failing signal.

1. Completion payload contract test.
   - Change `backend/tests/test_audit_payloads.py` so
     `CompletionPayload(incomplete_article_id=42,
     incomplete_article_status="open").model_dump(mode="json")` contains
     `incomplete_article_id` / `incomplete_article_status`.
   - Assert the dumped payload does not contain `pending_id` / `pending_status`.
   - This is a genuine RED-first test because current `CompletionPayload` has only
     `pending_id` / `pending_status`.

2. Completion ready-build audit tests.
   - Update ready-build skipped / failed assertions to expect
     `incomplete_article_id` / `incomplete_article_status` and the new outcome
     codes.
   - This locks the persisted audit contract, not only the Pydantic model.

3. Curation noise persistence tests.
   - Update curation repository / service tests to assert
     `CurationNoise.translated_title` / `summary`.
   - This is mostly a regression lock for the DB column rename and repository
     mapping (`translated_title=noise.title_ja`, `summary=noise.summary_ja`).
     It has lower RED value than the completion payload test because the DB rename
     itself is the primary change.

## Verification

Required checks:

```bash
cd backend && uv run ruff check app/ tests/
cd backend && uv run ruff format --check app/ tests/
cd backend && uv run pytest tests/ -x -q
```

Migration checks:

```bash
cd backend && uv run alembic upgrade head
cd backend && uv run alembic downgrade -1
cd backend && uv run alembic upgrade head
```

Targeted tests if full DB suite is too slow during local iteration:

```bash
cd backend && uv run pytest \
  tests/test_audit_payloads.py \
  tests/test_ai_analyzer.py \
  tests/analysis/curation/repository/test_curation_repository.py \
  tests/collection/article_completion/test_completer.py \
  tests/collection/article_completion/test_ready.py \
  tests/collection/article_completion/test_repository.py \
  tests/collection/article_completion/test_dispatch.py \
  tests/collection/test_scrape_html_body.py \
  tests/collection/article_completion/test_article_completion_audit_repository.py \
  tests/collection/article_completion/test_article_completion_failure_handler.py \
  tests/collection/article_completion/test_service.py \
  tests/collection/article_acquisition/test_repository.py \
  -q
```

Guard checks:

```bash
rg -n "\\b(title_ja|summary_ja)\\b" backend/app/models/curation_noise.py

rg -n "\\.(title_ja|summary_ja)\\b" \
  backend/tests/test_ai_analyzer.py \
  backend/tests/analysis/curation/repository/test_curation_repository.py \
  backend/tests/test_maintenance_tasks.py \
  backend/tests/test_maintenance_backlog.py

rg -n "pending_id|pending_status" backend/app backend/tests \
  -g '!backend/alembic/versions/**'
```

The curation model guard should return no hit.

The curation test attr guard should return no `CurationNoise` ORM read via
`.title_ja` / `.summary_ja`. Keyword arguments for AI/domain fixtures such as
`_noise_call(title_ja=..., summary_ja=...)` remain valid because the AI contract is
not renamed. `CurationNoise(...)` constructor keywords must use
`translated_title=` / `summary=`.

The completion guard should return no references where the value is
`incomplete_articles.id` / `incomplete_articles.status`. Remaining hits must be
explicitly allowlisted, for example:

- historical Alembic migrations (`pending_html_articles` history)
- specs / design docs describing old names
- unrelated backlog / maintenance "pending" concepts that do not point to
  `incomplete_articles.id`
- curation AI/domain `title_ja` / `summary_ja` hits are not part of the completion
  guard and remain valid by Decision 5.

## Done

- `curation_noises` has `translated_title` / `summary` columns.
- `curation_noises` no longer has current `title_ja` / `summary_ja` columns.
- `CurationNoise` ORM exposes `translated_title` / `summary`.
- curation noise save path writes `translated_title=noise.title_ja` and
  `summary=noise.summary_ja`.
- curation AI schema / domain result still use `title_ja` / `summary_ja`.
- New completion code uses `incomplete_article_id` for `incomplete_articles.id`.
- New completion code uses `incomplete_article_status` for
  `incomplete_articles.status` snapshot.
- Ready-build typed errors use
  `ArticleCompletionReadyBuildIncompleteArticleMissingError` and
  `ArticleCompletionReadyBuildIncompleteArticleNotRunningError`.
- New `CompletionPayload` dumps `incomplete_article_id` /
  `incomplete_article_status`.
- New `CompletionPayload` does not dump `pending_id` / `pending_status`.
- Historical `pipeline_events.payload.pending_id` /
  `payload.pending_status` are not rewritten.
- New ready-build outcome codes use `incomplete_article` names.
- Historical ready-build outcome codes are not rewritten.
- Task queue is drained / stopped before deploy so old `scrape_html_body` messages
  do not mix with the renamed task input.
- Ruff format / lint pass.
- Relevant tests pass, including migration upgrade / downgrade check.

## As-built 補足 (実装時に確定 / 合意済み)

新 outcome code `completion_ready_build_blocked_incomplete_article_not_running`
は 61 字で、既存 `pipeline_events.outcome_code` の `varchar(60)` を 1 字超える
(既存 outcome code の最長は 54 字)。命名を据え置くため、列幅を `varchar(80)` に
拡幅する。

- `backend/app/models/pipeline_event.py`: `outcome_code` を `String(80)` に。
- 同一 migration (`t3_curation_noise_rename`, down_revision =
  `x6_analyzed_article_ids`) の upgrade で `alter_column(... varchar(80))`、
  downgrade で `varchar(60)` に戻す。varchar 拡幅は postgres では metadata-only。
- 他 stage の outcome code も同じ列を共有するため横断的な余裕も得られる。
