# Agent run deadline fence slice

> 現行の処理・契約の配置は [Run周辺の概念境界](agent-run-concept-boundaries.md) を参照する。

Status: Draft

このスライスは、run作成時に60秒のdeadlineを固定し、`start_run()`と`complete_run()`を
そのdeadlineで制御するところまでを対象とする。

## Work definition

### Problem

現行の`start_run()`は`created_at`から求める別の180秒基準を使い、`complete_run()`は
run共通のdeadlineを確認しない。そのため、runの受付から60秒を超えた後も実行または
結果確定が成功し得る。

### Evidence

- `backend/app/models/agent_run.py`に`deadline_at`と`deadline_exceeded`がない。
- `backend/app/agent/runs/repository.py`の`start_run()`は固定180秒をその場で計算し、
  期限切れを`failed / stale`にする。
- 同じく`complete_run()`の確定条件は`running + attempt_epoch`だけで、deadline条件がない。

### Invariants

1. `deadline_at = created_at + 60 seconds`とし、run作成時に1度だけ固定する。
2. deadline判定はtransition評価時のPostgresの時刻を使い、lock待機前の古い時刻を使わない。
3. `db_now < deadline_at`だけをdeadline内とし、境界時刻ちょうどは期限切れとする。
4. retry、再配送、worker開始で`deadline_at`を更新しない。
5. deadline後のcandidate resultはassistant message、source、handoffを含めて永続化しない。
6. terminal transitionは一方だけが勝ち、既存terminalを上書きしない。

### Non-goals

- deadlineを超えたactive runを定期的に回収するsweeper。
- workerの残り時間に合わせた自己停止。
- `mark_failed()`、`mark_policy_blocked()`など他のterminal commandのdeadline fence。
- heartbeat、Redis lease、retry policy。
- SSE / pollingの方式変更。
- 日次quotaの計上基準の再設計。
- 既存のTaskiq timeout、running stale、frontend local deadlineの削除。

### Done

- 全runが不変の`deadline_at`を持つ。
- `start_run()`はdeadline前だけattemptを開始する。
- `complete_run()`はdeadline前のcurrent running attemptだけを確定する。
- deadline超過を`deadline_exceeded`として区別できる。
- deadlineとquotaの所有テストで、上記の契約を直接確認できる。

## Persisted contract

- `agent_runs.deadline_at`を`TIMESTAMPTZ NOT NULL`で追加する。
- 新規runの`created_at`と`deadline_at`は同じDB clockを基準に決める。
- 既存rowは`created_at + 60 seconds`でbackfillし、statusはmigration中に変更しない。
- terminal statusに`deadline_exceeded`を追加する。回答と`error_code`は持たない。
- 開始・終端時刻は記録しない。`started_at`・`completed_at`への依存は
  [時刻列の段階的廃止](agent-run-time-column-retirement-slice.md)で除去する。
- 新しいdeadline超過に`failed / stale`は使わない。
- APIのrun statusに`deadline_exceeded`を追加するが、`deadline_at`自体はpublic responseに追加しない。

## `start_run()` contract

active runに対し、DB上で1つの原子的なtransitionとして判定する。

```text
db_now < deadline_at
  -> status = running
  -> attempt_epoch += 1
  -> STARTED

db_now >= deadline_at
  -> status = deadline_exceeded
  -> attempt_epochは変更しない
  -> providerを呼ばない
  -> DEADLINE_EXCEEDED
```

- missingまたは既存terminal runは従来どおりidempotent skipとする。
- `queued`から`deadline_exceeded`になった場合は、現行互換としてquota予約を同じtransactionで返す。
- `running`の再配送が期限切れを検知した場合はquota予約を保持する。

## `complete_run()` contract

次をすべて満たすときだけ成功する。

```text
status = running
AND attempt_epoch = expected_attempt_epoch
AND db_now < deadline_at
```

- 成功時だけ、runの`completed`、assistant message、source、handoffを同じtransactionで確定する。
- current running attemptが`db_now >= deadline_at`なら、結果を保存せず、同じtransactionで
  runを`deadline_exceeded`にする。
- statusまたはepochが一致しない場合はtransition lostとし、runを変更せず結果を保存しない。
- callerは`COMPLETED`、`DEADLINE_EXCEEDED`、`TRANSITION_LOST`を区別できる契約とする。

## Behavior tests

このスライスでは、実DBを使う次の振る舞いテストをproduction codeより先に追加する。

### 1. `start_run()` behavior

- deadline前は`STARTED`となり、statusが`running`、epochが1つ進む。
- 境界時刻ちょうどは`DEADLINE_EXCEEDED`となり、epochを進めない。
- 期限切れrunのstatusが`deadline_exceeded`になる。

### 2. `complete_run()` behavior

- deadline前の`running + current epoch`だけが`COMPLETED`となり、回答artifactを確定する。
- 境界時刻ちょうどのcurrent epochは`DEADLINE_EXCEEDED`となり、回答artifactを残さない。
- old epochはrunと回答artifactを変更しない。

### 3. quota compatibility

- queuedでdeadlineを超えた場合はquota予約を返す。
- runningの再配送がdeadlineを超えた場合はquota予約を保持する。

未実装のdeadline契約だけを原因とするredと、既存契約のgreenを分けて確認する。
migration、model、API schema、frontend status parserの専用テストはこのスライスで新規追加しない。
既存の回帰テストと通常のcheckは実装後に実行する。
