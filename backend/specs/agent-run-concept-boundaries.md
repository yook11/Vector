# Agent Run周辺の概念境界

## Problem

`runs` に日次利用枠、固定期限の処理、会話に保存する回答の変換・引用検査が集まり、
変更したい振る舞いの所有先が分かりにくい。
「1回のRunで使うもの」ではなく、管理する概念と処理の責務で配置を分ける。

## Evidence

- `AgentRun` は質問に対応する実行記録であり、同じRunにも複数のattemptがある。
- 日次利用枠はRun単位ではなく、ユーザーと日本時間の日付の組で共有する。
- 開始期限は受付時に固定し、回答開始後の保存・回収には回答開始時刻からの回収期限を使う。
- 回答本文・出典は会話のメッセージとして保存する。
- 受付・完了・queuedの期限切れ処理は、複数概念を同一トランザクションで確定する。

## 所有先

| 配置 | 責務 |
|---|---|
| `app/agent/daily_quota/` | 日次利用枠の方針、利用枠の観測 |
| `app/agent/daily_quota/release.py` | 単件・一括返却、返却結果 |
| `app/agent/daily_quota/reservation.py` | 日次利用枠の予約、予約SQL、予約結果、上限超過例外 |
| `app/agent/running/deadline/policy.py` | 受付時に固定する期限の計算 |
| `app/agent/running/deadline/deadline_exceeded.py` | DB時刻取得、期限超過の確定・回収、回収結果 |
| `app/agent/threads/result_mapper.py` | 回答結果から会話のメッセージ・出典への変換 |
| `app/agent/threads/citation_integrity.py` | 保存する回答と出典の引用整合性検査・警告 |
| `app/agent/runs/` | worker向けの質問取得、共有語彙・表示変換 |
| `app/agent/running/presentation.py` | 所有ユーザー向けの表示・配信を支えるRun読み出しとライブ接続情報型 |
| `app/agent/running/cancellation.py` | 所有者によるキャンセルの確定、必要な利用枠返却、キャンセル結果 |
| `app/agent/running/policy_block_recording.py` | ポリシー判定結果によるRunの終了記録 |
| `app/agent/running/failure_recording.py` | 実行中の失敗・キュー投入失敗の記録 |
| `app/agent/running/continuation.py` | Runの継続可否の判断、短いsessionで確認するprobeとキャッシュ |
| `app/agent/running/creation.py` | 質問・queuedのRunの作成、利用枠予約、作成結果・例外 |
| `app/agent/running/attempt_start.py` | Runの開始可否、実行世代の更新、開始結果・不成立理由 |
| `app/agent/running/answer_generation.py` | 回答生成の開始記録・再生成許可・継続確認 |
| `app/agent/running/completion.py` | 回答・出典・申し送りの保存とRun完了の確定、完了の成功・失敗理由 |
| `app/agent/runs/execution.py` | 1回のRun実行を続けてよいか。`Continue` / `Stop` |

日次利用枠の予約は `reserve_daily_quota()`、`_build_daily_quota_reservation_statement()`、
`DailyQuotaReservation`、`DailyRequestLimitExceededError` を `daily_quota/reservation.py` にまとめる。
受付から渡されたsessionで質問・Runと同時に確定し、予約関数内ではcommitしない。
上限・日付定義はpolicyに残す。
返却は `release_daily_quota()`、`release_daily_quotas()`、`DailyQuotaReleaseOutcome` を
`daily_quota/release.py` にまとめ、旧persistence / contractsは削除し再公開しない。
返却対象と二重返却防止は呼び出し元のRun状態遷移が担い、返却関数は同じsessionでカウンターを更新する。
単件返却は既存のenum、一括返却は更新できたユーザー・日付の組の集合を返し、内部でcommitしない。
観測の位置は維持し、開始時の期限切れ返却ではcommit前に観測する。
旧モジュールから予約処理・結果・例外を委譲・再公開しない。

Runの作成は `AgentRunCreationRepository`、開始は `AgentRunAttemptStartRepository`、完了は `AgentRunCompletionRepository` が、
各概念の処理を呼び出し元から受け取った同じsessionで組み合わせる。
`create_user_run()`、`CreatedAgentRun`、`ThreadNotFoundError`、`ActiveRunConflictError` は
`running/creation.py` にまとめ、作成専用の実行中Run確認・連番取得も同じリポジトリが所有する。
routerが質問・Run・利用枠予約を同時に確定し、commit後にキューへ投入する。
workerによる各回の実行開始は既存Runの実行世代を更新し、質問・Run・利用枠予約を作り直さない。
旧repository / contractsから作成処理・結果・例外を委譲・再公開しない。
`start_run()`、`StartRunFailure`、`StartRunFailureReason` は `running/attempt_start.py` に置く。
開始成功は正の整数の実行世代を返し、不成立は理由付きの `StartRunFailure` を返す。
workerの `_start_run()` は開始リポジトリを使用し、sessionとトランザクションを引き続き所有する。
旧repository / contractsから開始処理を委譲・再公開しない。
`complete_run()`、`RunCompletionSuccess`、`RunCompletionFailure`、
`RunCompletionFailureReason`、保存時のロック待機上限は
`running/completion.py` に置き、旧repository / contractsからの委譲・再公開は残さない。
workerは完了処理に限って `AgentRunCompletionRepository` を使用し、
トランザクション、保存失敗時の処理、commit後の終了通知を引き続き所有する。
回答工程の `RunResult` は生成結果であり、保存の確定結果とは分ける。
ポリシーによる終了記録は `AgentRunPolicyBlockRepository.mark_policy_blocked()` が所有する。
runningかつ現在の実行世代だけを更新し、成立をboolで返す。トランザクションは呼び出し元が所有する。
ポリシー判定そのものは含めず、利用枠・会話データを変更しない。
現状の呼び出し元はテストだけであり、workerへの接続や通知は追加しない。
旧repositoryからの委譲・再公開は残さない。
失敗記録は `AgentRunFailureRepository` が `mark_failed()` と `mark_enqueue_failed()` を所有する。
実行中の失敗はqueued / runningかつ現在の実行世代、キュー投入失敗は世代によらずqueuedだけを更新する。
両処理は更新が成立したかをboolで返し、利用枠を返却しない。
トランザクションはworker / routerが所有し、workerの終了通知は更新成立のcommit後に行う。
旧repositoryからの委譲・再公開は残さず、キャンセルとその利用枠返却は別の処理として維持する。
キャンセルは `AgentRunCancellationRepository.cancel_run_for_user()` と
`RunCancellationSuccess`、`RunCancellationFailure`、`RunCancellationFailureReason` を `running/cancellation.py` にまとめる。
所有権を条件にRunを更新し、queuedの更新成立時だけ元の予約日付の利用枠を同じsessionで返却する。
runningと終了済みの利用枠は返却せず、利用枠の内部実装は日次利用枠に残す。
routerがトランザクションを所有し、利用枠の観測とrunningの終了通知はcommit後に行う。
旧repository / contractsからの委譲・再公開は残さない。
回答生成の実行管理は `AgentAnswerGenerationRepository` と `AnswerGenerationRepository` Protocolを
`running/answer_generation.py` にまとめる。workerがsession factory・Run ID・実行世代を渡し、
composition経由で両回答サービスへ注入する。
回答生成の開始・再生成許可はリポジトリ内部の短いトランザクションで確定し、commit後に生成を許可する。
生成中の継続確認は状態・世代・開始記録を読むだけとし、元の開始期限では停止しない。
継続確認の2秒キャッシュと停止結果の保持は同じリポジトリが所有し、開始・再生成許可には使わない。
回答生成・再試行方針・15秒タイマーは回答サービスに残し、実行継続probeとは共通化しない。
表示・配信向けの読み出しは `AgentRunPresentationRepository` と
`OwnedAgentRunLiveContext` を `running/presentation.py` にまとめる。
RunとThreadを結合して所有ユーザーを条件に読み出し、不存在と他ユーザーはともにNoneを返す。
状態応答は既存のschemaと表示変換を使い、ライブ接続情報を新たにAPIへ公開しない。
状態更新・期限回収・commitは追加せず、routerとSSEの制御は維持する。
worker向け質問取得は既存の場所に残し、旧repository / contractsから表示・配信向け定義を再公開しない。
継続確認は `AgentRunContinuationRepository.decide_execution_continuation()` と
`AgentRunExecutionProbe` を `running/continuation.py` にまとめる。
状態・実行世代・元の開始期限で判断し、超過時は既存の `expire_run()` で確定する。
DB時刻の取得順序を維持し、行ロックや回答開始後の回収期限による判定を追加しない。
probeが短いsessionとトランザクション、2秒のContinueキャッシュとStop保持を所有する。
確認の例外時は既存のログ・メトリクスを記録してContinueをキャッシュする。
workerで呼ぶ位置と `runs/execution.py` の共通語彙は維持し、旧repository / probeから再公開しない。
期限管理は `running/deadline/` に置き、旧 `run_deadline` からの委譲・再公開は残さない。
`policy.py` は60秒の定義と期限計算、`deadline_exceeded.py` は単件・全体・スレッド単位の確定と結果を所有する。
DB時刻取得は同じファイルの関数に残し、時計専用ファイル・クラスを追加しない。
回答生成の時間定義は回答生成側に残し、各呼び出し元の時刻取得・ロック・commit・通知の順序を維持する。
期限切れ回収はworkerから `sweep_deadline_exceeded_runs(session)` を直接呼ぶ。
一括返却の対象は期限処理が決め、利用枠のカウンター更新は日次利用枠が所有する。
引用警告は保存を拒否する条件にせず、既存の警告内容を維持する。

`runs` の状態語彙は期限処理や会話の表示でも参照するが、各packageの
`__init__.py` から実装を再公開せず、repository同士の相互呼び出しは追加しない。

この配置は [threads / runs境界分離](agent-threads-runs-boundary-slice.md) の配置定義を更新する。
既存の期限・利用枠・会話保存の振る舞い契約は維持する。

## Invariants

- 受付時の `created_at + 60秒` を期限として固定し、再配送・再開始で延長しない。
- 開始はqueuedとrunningを対象とし、成功のたびに実行世代をDB内で1増やす。
  開始時に期限切れとなったqueuedの終了確定と利用枠返却は同じトランザクションで行う。
- 開始・完了・回収の遷移判断はロック取得後のDB時刻を使い、期限ちょうどは期限切れとする。
- 完了にはrunning・現在のattempt・回答開始記録を必要とし、回答開始時刻からの回収期限で判定する。
  元の開始期限を過ぎたことだけでは保存を拒否しない。
- 利用枠の受付日は既存の `statement_timestamp()` による観測時点で固定する。
  期限処理の `clock_timestamp()` と共通化しない。
- queuedのキャンセル・期限切れだけ予約した元の日付へ利用枠を返す。
  runningの予約を返さず、二重返却・負のカウンターを許さない。
- 所有者の確認、active Runの一意性、古いattemptの排除、終端状態の保護を維持する。
- 受付では質問・Run・利用枠、完了では回答・出典・Run・調査の申し送りを同時に確定する。
- 受付・実行開始・完了・失敗記録・キャンセルのトランザクションは既存のrouter / workerが所有する。
  回答生成の開始・再生成許可・DBでの継続確認は、回答生成リポジトリが短いトランザクションを所有する。
- quotaログ・メトリクスと終了通知は既存のcommit後の位置で実行する。

## Non-goals

- DB schema、API、認証・認可、利用上限・期限・返却条件の変更。
- 回答生成・ライブ配信・worker timeoutの再設計。
- Runの作成・実行開始・実行継続確認・回答生成の開始と継続・完了・失敗記録・キャンセル・ポリシーによる終了記録・表示配信向け読み出し以外の実行管理の再編、会話への変換・引用検査の移動、テスト全体の再配置。
- 汎用service、Unit of Work、互換用の再公開の追加。

## Done

- 各処理と結果契約が上記の所有先にあり、旧importがapp / testsに残らない。
- 期限回収、引用検査、回答変換のテストも所有先に配置する。
- 複数概念を通す受付・完了・競合テストはRunの振る舞いとして維持する。
- backend lint / format、非integrationテスト、DB integrationテストが通る。


## 完了処理の結果とトランザクション

`complete_run()` は `RunCompletionSuccess | RunCompletionFailure` を返す。
成功型はフィールドを持たず、失敗型は `reason: RunCompletionFailureReason` を持つ。
両方とも変更不能なdataclassとする。成功はcommit前の完了処理の正常終了を意味する。

事前条件は以下の順序で評価し、最初に該当した理由を返す。

1. `RUN_NOT_FOUND`: RunとThreadの結合取得結果が存在しない。
2. `NOT_RUNNING`: Runがrunningではない。
3. `ATTEMPT_MISMATCH`: 実行世代が一致しない。
4. `ANSWER_NOT_STARTED`: 回答開始記録がない。
5. `DEADLINE_EXCEEDED`: 回答開始からの回収期限に到達している。

保存後の条件付き更新件数が1件でない場合は、原因を推測せず
`TRANSITION_LOST` を返す。DB障害などの予期しない例外は伝播させる。

workerは成功時にcommitし、期限超過時には期限切れ状態への変更をcommitする。
それ以外のFailureでは途中の回答・出典の書き込みが残り得るため、workerが明示的に
rollbackし、同じトランザクション内のDB操作を終了する。
repository内のcommit / rollbackやSAVEPOINTは追加しない。
完了・期限切れの通知はcommit後のみ行い、その他のFailureでは通知しない。
Failureのログには固定値のreasonを記録する。
保存やcommitの例外ではrollback後に既存の障害処理を行う。


## キャンセル結果の成功・失敗契約

`cancel_run_for_user()` は `RunCancellationSuccess | RunCancellationFailure` を返す。
Successは今回の操作でqueued / runningのRunをキャンセルしたことを表し、
`running_attempt_epoch`、必須の`quota_release_outcome`を持つ。
runningではキャンセルUPDATEが返した正の実行世代、queuedではDBの世代にかかわらず
戻り値の世代をNoneとする。戻り値はDBのattempt_epochを変更しない。
routerは戻り値の世代がある場合だけcommit後に終了通知を送る。
利用枠返却は世代の有無ではなく、queuedのキャンセルUPDATE成功を根拠に行う。
Failureは今回の操作ではキャンセルしなかったことを表し、reasonだけを持つ。
両型とも変更不能なdataclassとする。

- `RUN_NOT_FOUND`: 対象なし・他ユーザーのRun。APIは404。
- `ALREADY_COMPLETED`: 完了済み。APIは既存detailを維持して409。
- `ALREADY_FAILED` / `ALREADY_POLICY_BLOCKED` / `ALREADY_DEADLINE_EXCEEDED`:
  停止済み。APIは204。利用枠の観測・終了通知は行わない。

UPDATE成功時の利用枠返却とRun変更はrouterの同一トランザクションで確定し、
Successの場合のみcommit後に利用枠の観測とrunningのSSE終了通知を行い204を返す。
利用枠の不整合はキャンセル失敗に変換せず、成功型の返却結果として保持する。
両UPDATEが不成立でも最後のSELECTがactiveなどの想定外状態を返した場合は、
固定メッセージ `cancel run encountered an unexpected state` のRuntimeErrorを投げる。
この経路は従来の404からサーバーエラーとなり、DB例外と同様にrouterの
トランザクションをrollbackする。repository内でcommit / rollbackやSAVEPOINTを追加しない。
