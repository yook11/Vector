# Stage 4 Assessment: 監査主語を Ready から外し facts 由来で運ぶ

## 位置付け

Stage 5 embedding の型純度リファクタ（#888）と同じ動機（処理前提の型に監査用 id を
載せない）を Stage 4 に適用する。ただし **Stage 5 と実装を揃えない**。Stage 5 は
`analyzed_article_id → curation` の JOIN 回避のため相関 id を trigger に載せたが、Stage 4 は
`facts.analyzable_article_id` を precondition facts の一部として無料かつ authoritative に読める。
よって Stage 4 では trigger に載せず、facts 由来の値を Ready の外で返すだけにする。

**核心の再定義**: `analyzable_article_id` は tracing の correlation id ではなく、
記事軸で監査・追跡するための **nullable な監査主語（lineage subject）**。「観測のため常に
埋める値」ではない。authoritative に分かるときだけ記録し、分からない/不確かなときは
無理に焼かない。これに従い、監査主語を埋めるために message（trigger）を汚さない。

## Problem

`ReadyForAssessment`（[ready.py](../app/analysis/assessment/domain/ready.py)）は「assessor 入力と
Stage 4 precondition を満たした不変オブジェクト」と名乗りながら、処理前提ではない監査主語
`analyzable_article_id` を field に持っていた。型が「この処理に入る時点でどの前提が満たされて
いるか」を表すという役割に照らして、監査主語の混入は責務違反。

## Evidence

- `ready.analyzable_article_id` の消費先は観測（監査主語 / span / log）+ Stage 5 への引き継ぎで、
  assessment の判定・永続化には使わない:
  - [tasks/assessment.py:91](../app/queue/tasks/assessment.py#L91) `stage.set_article_id(...)`
  - [tasks/assessment.py:104](../app/queue/tasks/assessment.py#L104) rate-limit skip ログ
  - [audit/stages/assessment.py](../app/audit/stages/assessment.py) 成功 / 失敗監査の `article_id`
  - [tasks/assessment.py:139](../app/queue/tasks/assessment.py#L139) in-scope 成功後の `EmbeddingTrigger` へ引き継ぎ（#888）
- 永続化は `curation_id` 起点で **analyzable_article_id 未使用**:
  [analyzed_article.py:32-37](../app/analysis/analyzed_article.py#L32) /
  [repository.py save_in_scope/save_out_of_scope](../app/analysis/assessment/repository.py#L108)
- `facts.analyzable_article_id` は `ArticleCuration` の列で **JOIN 不要・無料・authoritative**:
  [repository.py:62-104](../app/analysis/assessment/repository.py#L62)
- blocked 経路は既にこの原則を体現:
  [ready.py](../app/analysis/assessment/domain/ready.py) が `ALREADY_*` にだけ
  `facts.analyzable_article_id` を載せ、`CURATION_MISSING`（主語未確定）には載せない。
  task は `is_idempotent_skip`（`ALREADY_*`）を監査せず log のみ、`CURATION_MISSING` だけ
  REJECTED を焼く（主語 None）。

## 合意済みの設計判断

1. **`analyzable_article_id` は nullable な監査主語**（correlation id ではない）。authoritative に
   分かるときだけ記録し、分からなければ焼かない。
2. **Ready から監査主語を外し、`try_advance_from` が facts 由来値を Ready の外で返す**（tuple）。
   `try_advance_from` に trigger hint を持たせない。ドメイン概念（Ready）を構築する責務に
   「trigger 値の照合」を混ぜて濁らせないため。
3. **`AssessmentTrigger` は `curation_id` のみ**。監査主語を埋めるために message を汚さない。
   message 契約は不変なので無停止は自明（optional / migration / Phase 2 は不要）。
4. **blocked 経路は不変**。`CURATION_MISSING` に主語を焼かない（`pipeline_events.article_id` は
   FK。主語が確定できない経路で無理に埋めると誤主語 + FK insert リスク）。`ALREADY_*` の
   idempotent skip も据え置き。
5. **監査は ready を残しつつ主語 id だけ明示引数化**。成功 / 失敗監査は `ready.summary`
   （input_text）/ `ready.curation_id` を payload に正当に使う（＝何を assessment したか）。
   よって ready は残し、主語 `article_id: int` だけを `ready.analyzable_article_id` に代えて
   明示引数で渡す。

## Invariants

- `pipeline_events.article_id` は完全な correlation id ではなく、記事軸で監査可能な場合にだけ
  記録する **nullable な監査主語**。
- 経路ごとの記録方針（origin/main の挙動を維持）:
  - **成功**（in-scope / out-of-scope）: facts 由来の元記事を `article_id` に記録する。
  - **`CURATION_MISSING`**: REJECTED を焼くが主語未確定なので `article_id` は None。
  - **`ALREADY_*`**: 勝者の行と冗長な idempotent skip。監査行を焼かず log のみ（例外は
    facts 由来 id を持つが task が監査に落とさない）。
- `analyzable_article_id` は不変 lineage。監査主語の値は従来（`ready.analyzable_article_id`
  = facts 由来）と同一に保つ。
- success path が返す監査主語は常に int（facts 非 None のときのみ Ready 構築成功、
  `CURATION_MISSING` は blocked で到達前に return）。よって `set_article_id(int)` に None を
  渡さない。

## Non-goals

- `AssessmentTrigger` / enqueue 元の変更（監査主語は trigger に載せない）。
- `CURATION_MISSING` 監査への主語付与（挙動据え置き）。
- 監査 `article_id` 軸の意味変更。
- Stage 5（embedding）の再変更。
- Phase 2 / migration（message 契約不変なので不要）。

---

## Changed Files（本 PR・5 ファイル）

1. [domain/ready.py](../app/analysis/assessment/domain/ready.py): `ReadyForAssessment` から
   `analyzable_article_id` field を削除。`try_advance_from` の戻り値を
   `tuple[ReadyForAssessment, int]` に変更し、`(ready, facts.analyzable_article_id)` を返す。
   blocked 例外（`ALREADY_*` に facts 由来 id、`CURATION_MISSING` は None）は不変。
2. [tasks/assessment.py](../app/queue/tasks/assessment.py): `ready, analyzable_article_id =
   await try_advance_from(...)` の tuple 受け。`set_article_id` / rate-limit ログ /
   `svc.execute` / `handler.handle` / `EmbeddingTrigger` を facts 由来の `analyzable_article_id`
   に。late-binding コメント削除。
3. [service.py](../app/analysis/assessment/service.py): `execute(self, ready, assessor, *,
   analyzable_article_id: int)`。`append_in_scope` / `append_out_of_scope` に
   `article_id=analyzable_article_id`。
4. [failure_handling.py](../app/analysis/assessment/failure_handling.py): `handle(...)` /
   `_audit_failure` / `_audit_unexpected_failure` に `analyzable_article_id: int` を伝播。
5. [audit/stages/assessment.py](../app/audit/stages/assessment.py): `append_in_scope` /
   `append_out_of_scope` / `append_failure` / `append_unexpected_failure` /
   `_append_failed_event` に `article_id: int` を明示引数化（ready の summary / curation_id は
   payload 用に残す）。`append_ready_build_blocked` / `append_backfill_assessment_aged_out` は据え置き。

`AssessmentTrigger`（messages/assessment.py）/ `tasks/curation.py` / `tasks/backfill.py` は
**変更しない**（origin/main のまま）。

---

## Tests

- `try_advance_from` の戻り値 tuple 化に追随（unpacking + mock の戻り値を `(ready, id)` に）。
- `ReadyForAssessment(...)` 構築から `analyzable_article_id=` を除去（field 削除）。positive
  validator テストも削除。
- 監査 / service / handler 呼び出しに `article_id=` / `analyzable_article_id=` 明示引数を追加。
- 監査主語の値が従来（facts 由来）と同一に焼かれることの確認。
- Stage 4→5 chain の `EmbeddingTrigger(analyzed_article_id=..., analyzable_article_id=...)` が
  facts 由来の監査主語を引き継ぐこと（[test_tasks.py](../tests/analysis/assessment/test_tasks.py) の
  chain assertion）。
- **取りこぼし防止**: `rg "ReadyForAssessment\(" backend/tests` = 9 ファイル:
  `tests/test_ready_for_classification.py`（domain test）, `tests/test_ai_analyzer.py`,
  `tests/analysis/test_analyzed_article.py`, `tests/analysis/assessment/test_tasks.py`,
  `test_assess_task_dispatch.py`, `test_service.py`, `test_failure_handler.py`,
  `test_assessment_audit_repository.py`, `test_assessment_repository.py`。
- `AssessmentTrigger(...)` を assert するテストは trigger 不変のため **更新不要**。

## Verification

```
rg "ReadyForAssessment\(" backend/tests
uv run pytest \
  backend/tests/test_ready_for_classification.py \
  backend/tests/test_ai_analyzer.py \
  backend/tests/analysis/test_analyzed_article.py \
  backend/tests/analysis/assessment/ -q
```

観点:
- `try_advance_from` が `(ready, facts.analyzable_article_id)` を返し、Ready に監査主語 field が無いこと。
- 監査 3 経路（in-scope / out-of-scope / failure）が明示 `article_id` で従来と同じ主語を焼き、
  payload の input_text / input_text_length（ready.summary 由来）が退化しないこと。
- blocked（`ALREADY_*` / `CURATION_MISSING`）の挙動不変（`CURATION_MISSING` は主語なし）。
- Stage 4→5 chain の `EmbeddingTrigger` が facts 由来の監査主語を引き継ぐこと。

## Done

- `ReadyForAssessment` が処理前提（curation_id / translated_title / summary）だけを持つ。
- 監査主語は facts から authoritative に読んだ値を Ready の外で運び、明示引数で
  span / log / audit / Stage 5 chain に渡す。message は汚さない。
- 監査 3 経路が従来と同じ主語を焼く。blocked 経路は挙動不変（主語未確定なら焼かない）。
- `/check` と上記 pytest が green。
