# Agent run deadline sweeper

Status: 実装済み

## Problem / Evidence

workerが終了処理に到達しなくても、期限切れのactive runをDBで終端させる。
変更前のsweeperはqueuedを`created_at + 300秒`、runningを
`started_at + 180秒`で`failed / stale`にしており、既存のdeadline契約と一致していなかった。
`sweep_deadline_exceeded_runs()`に統一し、保存済みの`deadline_at`だけを期限として読む。

対象は`backend/app/agent/runs/repository.py`、同`contracts.py`、
`backend/app/queue/tasks/agent_run.py`と関連テスト。
前提は[deadline fence仕様](agent-run-deadline-fence-slice.md)とする。
旧timeout仕様のsweeper判定は、この仕様で置き換える。

## 時刻の語意

| 名前 | 意味 |
|---|---|
| `created_at` | run受付時刻。作成時に`deadline_at`を決める材料 |
| `deadline_at` | このrunの結果を受理できる最終時刻 |
| `now` | その場でPostgresから取得する現在時刻。DB列でも新しいドメイン概念でもない |

- `deadline_at = created_at + RUN_DEADLINE_SECONDS`を作成時に固定する。現在は60秒。
- retry・再配送・worker開始・sweepで期限を延長しない。
- `RUN_DEADLINE_SECONDS`を参照するのは作成処理だけ。sweeperは秒数を持たず、保存済みの期限を読む。
- start / complete / sweepの現在時刻は`now`と呼ぶ。
  テストの`now=`は同じ現在時刻の差し込みである。
- terminal更新の判断にはlock取得後のDB時刻`now`を使う。
  候補取得もDB時刻で絞るが、lock待機前の時刻を終端更新へ使い回さない。
- 開始・終端時刻は記録しない。`started_at`・`completed_at`の旧列を残す期間も
  アプリから読み書きしない（[段階的廃止の仕様](agent-run-time-column-retirement-slice.md)）。

## Invariants — sweeperの契約

```text
now <  deadline_at → 期限内。変更しない
now >= deadline_at → queued / runningだけをdeadline_exceededへ更新する
```

- 境界時刻ちょうども期限切れ。`created_at`や`started_at`から期限を再計算しない。
- `deadline_exceeded`は回答・`error_code`を持たない。epochを進めず、回答・source・handoffを生成・保存しない。
- complete・start・cancel・別sweepと競合しても、terminal遷移は一方だけが勝つ。
  既存terminalを上書きせず、繰り返し実行しても確定結果を更新しない。
- queuedから期限切れを確定した場合だけ、同じtransactionで既存のquota予約を最大1回返す。
  runningの予約は保持し、quota不整合時の既存の扱いも変えない。
- 現行のrunning向けterminal通知はDB commit後に`deadline_exceeded`をbest-effortで送る。
  通知失敗でDBを戻さず、GETでも同じ終端状態を取得できる。
- sweeperの関数・結果型・ログ・関連テストもdeadlineの語彙へ揃え、旧180 / 300秒のstale判定を除去する。

sweepの起動間隔と受理期限は別物。60秒ちょうどのDB終端化は保証せず、次のsweepで収束する。
その間も`complete_run()`のdeadline fenceにより、期限後の結果は受理されない。

## Non-goals

- DB列の追加・削除、`completed_at`の改名、migration・index追加。
- worker自己停止、Taskiq / application timeoutの撤去、heartbeat / lease、retry設計。
- `mark_failed()`等へのdeadline fence追加、日次quotaの計上基準の再設計。
- sweepの起動間隔、SSE / polling / 再接続、frontend待機処理、公開APIの変更。
- sweeper以外の`stale`語彙の一括改名。

## テスト先行 / Done

今回追加するRepositoryの振る舞いテストは次の2つだけとし、未実装の契約によるredを確認してから実装する。

1. `now < deadline_at`なら何もしない。
2. `now >= deadline_at`なら`deadline_exceeded`にする。

どちらもqueued / runningで確認し、期限切れ側は境界ちょうどと超過を含める。
quota・既存terminalの保護・競合・Task通知は既存テストで回帰確認し、新しい独立テスト群は追加しない。

旧180 / 300秒のカットオフ、`started_at`による期限計算、`failed / stale`回収の専用テストは削除する。
定数・AST・ソースコード・時計関数の呼び方・SQL更新回数を固定する検査も削除する。
旧期限テストに混在していた終端保護とquotaの保証は、永続結果を確認する既存テストへ整理して残す。
テストは`test_deadline_sweep.py`、`test_deadline_sweep_task.py`、`test_deadline_sweep_contract.py`に配置する。

既存のstart / completeの保証を維持し、上記テストと影響範囲の`/check`が通れば完了。
