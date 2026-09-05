# スレッド表示時の期限切れrunの回収

Status: Implemented
Updated: 2026-09-05
Scope: 所有するスレッドの詳細取得時に、対象スレッドの未終了runを期限判定し、確定した状態を返す

## Problem

ユーザーがスレッドを開いたとき、定期回収を待っている期限切れrunを実行中として表示し続けない。
対象スレッドの期限切れrunをバックエンドで`deadline_exceeded`に確定し、その状態を詳細レスポンスへ反映する。
質問やrunを削除・非表示にする機能ではない。

## 対象リクエストと処理順序

対象は既存の`GET /api/v1/research/threads/{thread_id}`（`get_research_thread`）とする。
画面表示のための詳細取得を起点とし、同じ詳細取得リクエストの再送・再取得にも同じ契約を適用する。
画面表示専用の追加リクエストや新規endpointは作らない。

1. 認証済みユーザーが指定スレッドを所有していることを確認する。
2. 対象スレッドの未終了runについて、DB上の状態と期限を確認する。
3. 回収条件を満たすrunがあれば、必要なquota処理と同じ短いトランザクションで終了を確定する。
4. 回収をコミットした後、更新後の状態を使ってスレッド詳細を構築して返す。

詳細取得は期限到達済みの状態をDBへ反映する操作を伴う。再送・重複取得で終了状態を変更し直したり、
quotaを重複返却したりしない。確認が完了した後に期限へ達するrunを、将来の時刻を先取りして終了させない。

## Invariants

### 1. 開いたスレッドだけを対象にする

- 認証済みの`user_id`とリクエストの`thread_id`に対象を限定する。
- 所有権が確認できないスレッドは、存在しないスレッドと同様に既存の404を返す。
- 所有権確認前に、そのスレッドのrunを更新しない。
- 同じユーザーの別スレッド、他ユーザーのスレッドには回収・quota更新・通知を行わない。
- 全体回収を実行してから対象スレッドを抽出する方式にしない。
- 対象runがない場合は、通常のスレッド詳細を返す。

### 2. 定期回収と同じ期限条件を使う

期限の契約は[回答生成の開始期限・回答確定・未完了runの回収](./answer-generation-deadline.md)に従う。

| DB上の状態 | 回収条件 |
|---|---|
| `queued` | `DB現在時刻 >= deadline_at` |
| `running`かつ`answer_started_at`なし | `DB現在時刻 >= deadline_at` |
| `running`かつ`answer_started_at`あり | `DB現在時刻 >= answer_started_at + 工程制限時間 + 回収猶予` |
| 終了済み | 回収しない |

- 期限ちょうどは回収対象とする。
- 回答生成開始済みのrunを、元の60秒だけでは回収しない。
- 時刻はDBを正本とし、ブラウザやワーカーのwall clockで終了を確定しない。
- 工程制限時間と猶予は`answering/timing.py`の共通定義を参照する。確認時点では15秒＋30秒である。
- この取得経路に独立した秒数や期限計算を追加しない。定期回収と対象の絞り込みだけが異なる実装にする。

### 3. 状態確認と終了更新を競合から保護する

- 対象runのロック取得後、最新の状態・実行世代・開始記録と、その後に取得したDB時刻で判断する。
- ロック待機中に回答生成が始まった場合は、開始済みの期限で再判定する。
- 完了・失敗・キャンセル・定期回収が先に確定した場合は、その終了状態を維持する。
- 確認した実行世代に対する更新として保護し、古い候補情報で別の世代を終了させない。
- 今回の回収が先に確定した場合、遅れて戻ったワーカーの保存で上書きさせない。
- 競合により回収対象でなくなったことを、回収失敗として扱わない。
- 終了確定までに必要な短い範囲だけロックし、通知送信や詳細レスポンスの組み立てのために保持しない。

### 4. 回収の更新内容とquotaを維持する

- 回収対象のrunを`deadline_exceeded`にし、`assistant_message_id`と`error_code`は`None`とする。
- `created_at`、`deadline_at`、`answer_started_at`、`attempt_epoch`、質問・回答履歴・出典・handoffを変更しない。
- 期限回収を理由にスレッドの`updated_at`や一覧の並び順を変更しない。
- queuedのquota返却は、既存の返却条件に従ってrunの終了更新と同じトランザクションで行う。
- runningのquota予約は返却しない。
- quota未記録のlegacy run、counter欠損・underflowの扱いは定期回収と揃える。
  counter不整合ではrunの終了を優先し、不正な減算は行わず既存の方法で観測する。
- 重複リクエストや定期回収との競合によって、quotaを二重返却しない。

### 5. コミット済みの状態を返す

- `ResearchThreadDetail`とメッセージ内のrun情報は、既存のPydantic schemaとprojectionを使用する。
- 回収を行ったrunは、同じリクエストのレスポンスで`deadline_exceeded`として返す。
- ORMのidentity mapや回収前に組み立てたDTOによって、古い`running`を返さない。
- 期限内のrunは実行中のまま返し、終了済みrunはその状態のまま返す。
- 質問・runを隠さず、既存の期限切れ表示へ渡す。部分回答を確定した回答として保存しない。
- APIのfield、status値、メッセージ順序、404の公開契約を変更しない。
- frontendは返却されたDB状態を採用し、クライアント時刻だけでrunを終了扱いにしない。
- キャッシュだけで画面を描画する場合は回収が起動しないため、スレッドを開く詳細取得経路が使われることを検証する。

### 6. 回収失敗と終了通知

- DB更新やコミットに失敗した場合はロールバックし、既存の安全なサーバーエラー経路に従う。
  未確定の`deadline_exceeded`を成功レスポンスとして返さない。
- 回収と詳細読み取りは別段階であり、回収コミット後に詳細取得が失敗しても、確定した終了状態は維持する。
- 回収したrunning runには、DBコミット後に既存の終了イベントをbest-effortで通知する。
- 通知失敗で回収をロールバックしたり、取得リクエストを失敗にしたりしない。
- 終了状態の正本はDBと詳細レスポンスとし、別画面は既存の通知・polling経路で収束する。
- 再取得時にすでに終了済みのrunについて、回収成功としてquota更新・通知を繰り返さない。

## 責務と負荷の境界

- スレッド詳細取得のユースケースが、所有権確認・対象スレッドの回収・詳細取得の順序を管理する。
- `run_deadline`側が、定期回収と共通の期限判定および終了更新を担う。
- スレッドのprojectionや汎用的なreadメソッドに、暗黙の回収処理を混在させない。
- 対象スレッドの未終了runに限定したDB操作とし、他スレッドの履歴を走査しない。
- スレッド一覧取得、run単体のpolling、SSE接続・再接続、画面の再描画には新しい回収処理を追加しない。
- 定期回収のスケジュールは維持する。今回のアクセス増加を理由に、検証なしで定期回収の頻度を下げない。
- スレッド一覧と詳細は別リクエストであり、並行取得された一覧の`has_active_run`との同時点一致は保証しない。
  対象スレッドの回答表示・実行中判定は更新後の詳細を使用する。

## Evidence

- [スレッド詳細取得endpoint](../../backend/app/agent/router.py): 現在は所有者限定の詳細readを呼ぶ。
- [スレッドrepository](../../backend/app/agent/threads/repository.py): 所有権確認後にメッセージとrunを取得する。
- [詳細projection](../../backend/app/agent/threads/projection.py): ユーザーメッセージにrun状態を投影する。
- [公開API schema](../../backend/app/schemas/research.py): `ResearchThreadDetail`の正本。
- [期限回収](../../backend/app/agent/run_deadline/persistence.py): 開始記録に応じた条件、quota、回収結果を扱う。
- [回答生成の時間定義](../../backend/app/agent/answering/timing.py): 工程制限時間と回収猶予の共通定義。
- [定期回収タスク](../../backend/app/queue/tasks/agent_run.py): 回収commit後の観測・終了通知を扱う。
- [frontendの詳細取得](../../frontend/src/features/research/api/get-research-thread.ts): `cache: "no-store"`で取得する。
- [画面用データ取得](../../frontend/src/features/research/page-models/research-thread.ts): 詳細と一覧を並行取得する。

期限判定は `answering/timing.py` と `_has_reached_recovery_deadline` を共用し、この経路に独立した秒数は置いていない。

## Non-goals

- 全ユーザー、同じユーザーの全スレッドの一括回収。
- 回答生成・再生成・保存の期限契約、工程タイマー、猶予値の変更。
- 期限ごとの予約タスク、定期回収の頻度調整、生成中の継続確認間隔の変更。
- handoffの非同期化、自動再生成、自動再送。
- runやメッセージの削除・非表示、新しい期限切れUIの導入。
- DB schema、新規dependency、公開レスポンスshape、認証・所有権ポリシーの変更。

## Verification

時間境界はDB時刻を制御して検証し、実時間で60秒・45秒待つテストにしない。

| ケース | 期待する結果 |
|---|---|
| 所有スレッドの期限切れqueued | DBと同じレスポンスで`deadline_exceeded`、対象ならquota返却 |
| 所有スレッドの未生成runningが元の期限ちょうど | `deadline_exceeded`、quota返却なし |
| 生成開始済みで元の60秒経過後、新しい期限より前 | `running`を維持 |
| 生成開始済みで新しい期限ちょうど・超過後 | `deadline_exceeded`、quota返却なし |
| 期限直前・未終了runなし・終了済みrunのみ | 状態・quota・履歴を変更せず詳細取得成功 |
| 同じユーザーの別スレッドにも期限切れrunあり | 開いたスレッドだけを変更 |
| 他ユーザーのスレッド・存在しないスレッド | 404、run・quota・通知の副作用なし |
| 同じ詳細取得の繰り返し・並行実行 | 終了確定とquota返却は一度だけ |
| ロック待ち中に生成開始が確定 | 開始済みの期限を適用して再判定 |
| 保存・失敗・キャンセル・定期回収との競合 | 先に確定した終了状態を維持し、履歴の不整合・二重返却なし |
| DB更新・quota更新・commit例外 | ロールバック、未確定の終了状態を成功として返さない |
| 終了通知例外 | 回収状態を維持し、詳細は成功レスポンスを返す |
| quota未記録・counter不整合 | 定期回収と同じ結果・観測、負のcounterを作らない |
| 詳細レスポンス・画面表示 | 質問履歴は残り、期限切れrunを実行中として表示しない |
| スレッド一覧・run polling・SSE取得 | 新しいスレッド回収処理を起動しない |

関連する検証対象は[APIテスト](../../backend/tests/agent/test_router_research.py)、
[スレッドrepositoryテスト](../../backend/tests/agent/threads/test_repository.py)、
[定期回収テスト](../../backend/tests/agent/run_deadline/test_deadline_sweep.py)、
[画面データ取得テスト](../../frontend/src/features/research/page-models/research-thread.node.test.ts)、
[スレッド表示テスト](../../frontend/src/features/research/components/ResearchThreadView.test.tsx)とする。

2026-09-05 に実装し、以下を確認した。

- `sweep_deadline_exceeded_runs_for_thread` が開いたスレッド以外を更新しない
- `GET /api/v1/research/threads/{thread_id}` が所有確認後に既存回収を呼び、同じレスポンスで `deadline_exceeded` を返す
- 他ユーザー・存在しないスレッドは 404 で副作用なし
- 一覧・run 取得は新しい回収を起動しない
- 初期 `deadline_exceeded` は質問を残し、下書きを出さない

`/check`（2026-09-05）: backend ruff / unit 5253 passed / integration 1059 passed, 22 skipped。frontend biome / tsc / vitest 1336 passed。

## Done

- 対象の詳細取得から、所有スレッドだけの期限確認・回収が実行される。
- 定期回収と同じ期限条件を参照し、終了更新・quota・競合時の不変条件を満たす。
- 回収commit後の状態が同じリクエストの詳細と画面に反映される。
- 受け入れ条件をテストし、既存のAPI・定期回収を回帰させない。
- 実装変更後に`/check`を実行し、結果と実装への参照を本書へ追記する。

## Implementation

実装済み。

- スレッド限定の回収入口: [run_deadline/persistence.py](../../backend/app/agent/run_deadline/persistence.py) の `sweep_deadline_exceeded_runs_for_thread`
- 所有確認・回収commit・終了通知・詳細再読: [threads/detail.py](../../backend/app/agent/threads/detail.py) の `read_owned_thread_detail`
- 接続点: [router.py](../../backend/app/agent/router.py) の `get_research_thread`
- 絞り込みテスト: [test_deadline_sweep.py](../../backend/tests/agent/run_deadline/test_deadline_sweep.py) の `test_sweep_for_thread_leaves_other_thread_unchanged`
- 詳細取得の接続テスト: [test_router_research.py](../../backend/tests/agent/test_router_research.py) の `TestGetResearchThread`
- 初期 `deadline_exceeded` 表示: [ResearchThreadView.test.tsx](../../frontend/src/features/research/components/ResearchThreadView.test.tsx)
