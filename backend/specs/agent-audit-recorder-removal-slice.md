# Agent audit recorder 撤去 slice 仕様

更新日: 2026-07-18

実装状況: Implemented

## 位置付け

agent 3 flow（Planner / EvidenceAnswerFlow / DirectAnswerFlow）が持つ audit recorder 層
（Protocol + event model + 配管）を撤去する。

前提となる決定は「agent イベントの DB 永続化を今はやらない」。差し替え可能な sink が
存在しない以上、recorder を注入する理由が消え、Protocol・event model・サービス側の
`_record_*` 配管は同じ理由で同時に不要になる。

「どのように失敗したのか」の記録は撤去ではなく置き換える。

- 個票（attempt 単位の失敗詳細）: AgentRuntime の attempt span が既に記録している
  （`runtime/gemini.py` の `result` / `error_type` / `attempt_number` / model attribute）。
  本 slice で `prompt_version` attribute を追加する。
- 傾向（集計）: 既存 outcome metric 3 本に `failure_code` 次元を追加する。

本 slice は `planner-agent-runtime-slice.md` の `QuestionPlanningService` constructor 契約を
修正する。同 spec が固定した `audit_recorder: PlannerAuditRecorder | None = None` パラメータは
本 slice で失効する。

## Work Definition

### Problem

- `PlannerAuditRecorder` / `AnswerSynthesisAuditRecorder` / `DirectAnswerAuditRecorder` の
  3 Protocol に実装が存在せず（実装はテストの Fake のみ）、実装予定の slice も無い。
  production 配線は 3 箇所すべて `audit_recorder=None`。
- event model 8 つと、None 分岐・event 組み立て・best-effort swallow を担う配管関数群が、
  本番では「何もしない」パスとしてサービス/flow に散在し、制御フローの可読性を下げている。
- `ai_model` / `prompt_version` が event 専用の引数として全記録呼び出しへ引き回されている。
- 監査の既存原則「焼くかは、それを要する consumer がいるかで決める」に照らして、
  consumer 不在のまま記録層だけが存在している。
- 一方で outcome metric には失敗種別の次元が無く、「fallback の内訳」を集計で見られない。

### Evidence

- `app/agent/planning/audit.py:167` — `PlannerAuditRecorder(Protocol)`。実装は
  `tests/agent/planning/test_planner.py` の Fake / Raising のみ。
- `app/agent/answering/audit.py:290,303` — answering 側 2 Protocol。同様に Fake のみ。
- `app/agent/composition.py:150,159,165` — 3 flow とも `audit_recorder=None` で配線。
- `backend/specs/` 全体に具象 recorder を実装する slice が存在しない
  （agent-history 系 slice にも AuditRecorder は登場しない）。
- `app/agent/runtime/gemini.py:88-129` — attempt span が `agent_name` / `attempt_number` /
  model / `result`（provider_error / blocked / invalid_response）/ `error_type` を記録済み。
  event model が運ぶ個票情報とほぼ重複する。
- `app/agent/planning/metrics.py` / `app/agent/answering/metrics.py` — outcome counter 3 本。
  いずれも失敗種別の次元を持たない。
- `classify_planner_failure` / `classify_answer_synthesis_failure` /
  `classify_direct_answer_failure` の `request_retry_disposition` は retry 判定の制御フローに
  使われており、監査とは独立に必要。

### Invariants

- retry / fallback の policy は変えない。failure classification は引き続き retry 判定の
  正本であり、`previous_error` の feedback loop も維持する。
- outcome metric の既存 label（`result` / `retry_used` など）と metric 名は変えない。
  変更は `failure_code` attribute の追加のみ（後方互換な追加）。
- `failure_code` は classifier が返す `code`（provider error CODE / defect 値 /
  `unexpected_error`）に限定し、低カーディナリティを保つ。自由文の `failure_reason` を
  metric attribute に入れない。
- attempt span の `prompt_version` は `agent.prompt.version` から取り、呼び出しごとに
  変動する値を入れない。
- 撤去後の agent flow は recorder / sink への依存を一切持たない。イベント記録層の再導入は、
  それを読む consumer（管理画面・health reader 等）を定義した slice の合意を前提とする。

### Non-goals

- answering 2 flow への attempt span 追加。個票は両 flow が AgentRuntime へ移行する
  後続 slice（`agent-declaration-runner-orchestration-slice.md` の PR2 以降）で揃う。
  本 slice で暫定 span を作らない。
- answer synthesis の defect（決定的補修）可視化の代替。defect event / `defect_count` は
  consumer 不在のまま消す。必要になれば metric 追加を consumer 駆動で別途合意する。
- `RequestRetryDisposition` が planning / answering に重複定義されている件の統一。
- agent イベントの DB 永続化の設計。
- metric 名・既存 label の再設計。

### Done

- production コードから 3 Protocol・event model 8 つ・`_record_*` 配管・
  `audit_recorder` パラメータ・event 専用の `ai_model` / `prompt_version` 引き回しが消える。
- outcome metric 3 本が `failure_code` を持ち、fallback / failed 時に classifier の
  `code` が入る。
- AgentRuntime の attempt span に `prompt_version` が入る。
- 3 flow の retry / fallback 挙動を検証する既存テストが green のまま維持され、
  recorder 前提のテスト（Fake / Raising / event assert）は metric assert（capfire）へ
  置き換わる。
- `/check` が pass する。

## 変更内容

### 1. planning 側の撤去

`app/agent/planning/audit.py`

- 削除: `PlannerAuditRecorder`、`PlannerAttemptFailureEvent`、`PlannerDraftReceivedEvent`、
  `PlannerFinalEvent`、`PlannerOutcomeCode`。
- 維持: `RequestRetryDisposition`、`PlannerFailureAttributes`、`classify_planner_failure`。
- ファイル名を `failure.py` へ改名する。残る内容は監査ではなく failure classification
  （retry 判定の入力）であり、`audit` の語が実態と乖離するため。docstring の
  「audit attributes」も request-local failure attributes へ言い換える。

`app/agent/planning/service.py`

- 削除: `_record_draft_received` / `_record_attempt_failure` / `_record_plan_created` /
  `_record_final_event` / `_fallback_with_audit`、constructor の `audit_recorder`、
  `ai_model` / `prompt_version` ローカル変数。
- `_PLANNER_AUDITED_ERRORS` は「監査対象」ではなく「分類して policy 処理する境界例外」なので
  `_PLANNER_CLASSIFIED_ERRORS` へ改名する。
- fallback 経路は inline で `safe_fallback_plan()` を作り、
  `record_question_planner_outcome(result="fallback", ..., failure_code=failure.code)` を
  呼んで return する。planned 経路は既存呼び出しに `failure_code` なし（success）で揃える。

### 2. answering 側の撤去

`app/agent/answering/audit.py`

- 削除: 2 Protocol、`AnswerSynthesisAttemptFailureEvent`、`AnswerSynthesisDefectEvent`、
  `AnswerSynthesisFinalEvent`、`DirectAnswerAttemptFailureEvent`、`DirectAnswerFinalEvent`、
  `AnswerSynthesisOutcomeCode`、`DirectAnswerOutcomeCode`。
- 維持: `RequestRetryDisposition`、2 つの FailureAttributes、2 つの classify 関数、
  classify が参照する code 定数（`PYDANTIC_VALIDATION_FAILED` / `ANSWER_DRAFT_INVALID`）。
- planning と同じ理由で `failure.py` へ改名する。

`app/agent/answering/evidence_answer/flow.py`

- 削除: `_record_attempt_failure` / `_record_defect` / `_record_final_event`、
  constructor の `audit_recorder`、`_generator_attr` による `ai_model` / `prompt_version` 取得。
- `_record_synthesized` / `_fallback_with_audit` は event 組み立てを失い、
  metric 呼び出しと fallback 構築だけが残る。メソッドとして残すか inline 化するかは
  実装時の可読性で判断してよいが、`_fallback_with_audit` の名前から `audit` を外す。
- `_EVIDENCE_ANSWER_AUDITED_ERRORS` を `_EVIDENCE_ANSWER_CLASSIFIED_ERRORS` へ改名する。
- fallback 時の metric に `failure_code=failure.code` を追加する。
- defect ループ（`for defect in defects: _record_defect(...)`)は削除する。
  `defect_count` の集計変数も event 専用のため削除する。

`app/agent/answering/direct_answer/flow.py`

- 同型の撤去: `_record_attempt_failure` / `_record_final_event`、`audit_recorder`、
  `ai_model` / `prompt_version` 取得。`_record_answered` / `_record_failed` は
  metric 呼び出しへ縮退する。
- failed 時の metric に `failure_code=failure.code` を追加する。

### 3. composition

`app/agent/composition.py` — 3 箇所の `audit_recorder=None` を削除する。

### 4. metric 契約

`app/agent/planning/metrics.py` / `app/agent/answering/metrics.py`

- 3 つの record 関数へ `failure_code: str | None = None` を追加する。
- attribute には常に `failure_code` を含め、None は `"none"` として出す
  （success / failure で label set が変わると集計時に扱いにくいため、値で統一する）。
- docstring に「classifier の code のみを渡す（自由文禁止）」の制約を 1 文で残す。

### 5. runtime span

`app/agent/runtime/gemini.py` — `span_attributes` に
`"prompt_version": agent.prompt.version` を追加する。

### 6. テスト

- 削除: `FakePlannerAuditRecorder` / `RaisingPlannerAuditRecorder` /
  `FakeDirectAnswerAuditRecorder` / `RaisingDirectAnswerAuditRecorder` /
  evidence 側 Fake と、event 内容・swallow 挙動を assert するテスト。
- 置き換え: fallback / failed 経路で outcome metric に `failure_code` が入ることを
  capfire で assert する（`get_metrics_data` を 1 回読む既存パターンに従う）。
- 維持: retry 判定・fallback 内容・`previous_error` feedback などの振る舞い不変条件を
  検証する既存テスト。recorder 引数を渡している箇所は引数削除のみ行う。
- runtime span の `prompt_version` は既存の runtime span テストへ attribute assert を足す。

## 検証

- `/check`（backend lint / format / types / tests）。
- 対象テスト: `tests/agent/planning/test_planner.py`、
  `tests/agent/answering/evidence_answer/test_flow.py`、
  `tests/agent/answering/direct_answer/test_flow.py`、runtime span テスト。

## 記録の置き換え対応表

| 消える記録 | 置き換え先 |
| --- | --- |
| AttemptFailureEvent（attempt 単位の失敗種別） | runtime attempt span の `result` / `error_type`（planner は現行、answering は runtime 移行 slice で追随） |
| FinalEvent の失敗種別（fallback / failed の内訳） | outcome metric の `failure_code` |
| FinalEvent の outcome / retry_used | 既存 outcome metric（変更なし） |
| DraftReceivedEvent の query 件数 | 置き換えない（consumer 不在。必要になれば span attribute で再検討） |
| DefectEvent / defect_count | 置き換えない（Non-goals 参照） |
| event の `prompt_version` | runtime attempt span の `prompt_version`（本 slice で追加） |
