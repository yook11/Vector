# Agent Run周辺の概念境界

## Problem

`runs` に日次利用枠、固定期限の処理、会話に保存する回答の変換・引用検査が集まり、
変更したい振る舞いの所有先が分かりにくい。
「1回のRunで使うもの」ではなく、管理する概念と処理の責務で配置を分ける。

## Evidence

- `AgentRun` は質問に対応する実行記録であり、同じRunにも複数のattemptがある。
- 日次利用枠はRun単位ではなく、ユーザーと日本時間の日付の組で共有する。
- 期限は受付時に固定し、開始・完了の制限と期限切れ回収で同じ値を使用する。
- 回答本文・出典は会話のメッセージとして保存する。
- 受付・完了・queuedの期限切れ処理は、複数概念を同一トランザクションで確定する。

## 所有先

| 配置 | 責務 |
|---|---|
| `app/agent/daily_quota/` | 日次利用枠の方針、予約、単件・一括返却、利用枠の観測 |
| `app/agent/run_deadline/policy.py` | 受付時に固定する期限の計算 |
| `app/agent/run_deadline/persistence.py` | DB現在時刻、期限超過の確定、期限切れRunの回収 |
| `app/agent/run_deadline/contracts.py` | 期限切れ回収の結果 |
| `app/agent/threads/result_mapper.py` | 回答結果から会話のメッセージ・出典への変換 |
| `app/agent/threads/citation_integrity.py` | 保存する回答と出典の引用整合性検査・警告 |
| `app/agent/runs/` | Runの受付・開始・完了・キャンセル、有効なattemptの確認、状態の取得・公開 |

`AgentRunRepository` の受付・完了は、各概念の処理を同じsessionで組み合わせる。
期限切れ回収はworkerから `sweep_deadline_exceeded_runs(session)` を直接呼ぶ。
一括返却の対象は期限処理が決め、利用枠のカウンター更新は日次利用枠が所有する。
引用警告は保存を拒否する条件にせず、既存の警告内容を維持する。

`runs` の状態語彙は期限処理や会話の表示でも参照するが、各packageの
`__init__.py` から実装を再公開せず、repository同士の相互呼び出しは追加しない。

この配置は [threads / runs境界分離](agent-threads-runs-boundary-slice.md) の配置定義を更新する。
既存の期限・利用枠・会話保存の振る舞い契約は維持する。

## Invariants

- 受付時の `created_at + 60秒` を期限として固定し、再配送・再開始で延長しない。
- 開始・完了・回収の遷移判断はロック取得後のDB時刻を使い、期限ちょうどは期限切れとする。
- 利用枠の受付日は既存の `statement_timestamp()` による観測時点で固定する。
  期限処理の `clock_timestamp()` と共通化しない。
- queuedのキャンセル・期限切れだけ予約した元の日付へ利用枠を返す。
  runningの予約を返さず、二重返却・負のカウンターを許さない。
- 所有者の確認、active Runの一意性、古いattemptの排除、終端状態の保護を維持する。
- 受付では質問・Run・利用枠、完了では回答・出典・Run・調査の申し送りを同時に確定する。
- トランザクションは既存のrouter / workerが所有し、分割先でcommitしない。
- quotaログ・メトリクスと終了通知は既存のcommit後の位置で実行する。

## Non-goals

- DB schema、API、認証・認可、利用上限・期限・返却条件の変更。
- 回答生成・ライブ配信・worker timeoutの再設計。
- 汎用service、Unit of Work、互換用の再公開の追加。

## Done

- 各処理と結果契約が上記の所有先にあり、旧importがapp / testsに残らない。
- 期限回収、引用検査、回答変換のテストも所有先に配置する。
- 複数概念を通す受付・完了・競合テストはRunの振る舞いとして維持する。
- backend lint / format、非integrationテスト、DB integrationテストが通る。
