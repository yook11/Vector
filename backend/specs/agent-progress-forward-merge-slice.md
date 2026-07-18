# Agent run 進捗表示の前進保証（poll / SSE 合成規則）slice 仕様

更新日: 2026-07-18（Implemented）

実装状況: Implemented — 2026-07-18

## 位置付け

親仕様: `agent-answer-streaming-sse.md`（SSE 配信）、
`../../frontend/specs/agent-research-live-ui-slice.md`（Research live UI）、
前提 slice: `agent-attempt-epoch-fencing-token-slice.md`（Implemented）。

本 slice は frontend 仕様の合成規則 35 / 36 を更新する。poll がより大きいepochを観測した場合は
`currentAttemptEpoch`を切り替え、attempt-localなdraft / generationをresetし得るため、poll がそれらを
変更しないという旧 rule 35 も成立しない。接続モードを代理変数にした旧 rule 36 の採用判定を、attemptEpochと
stageの前進方向による判定へ置換する。

### 責務の固定

- **DB**: attempt 帰属付きの最新 status / progress_stage / terminal の正本。
- **Redis Stream / SSE**: 低遅延の stage / activity / draft 配信。
- **Redis List（`recentEvents`）**: polling-only 時を主とし、SSE未受理の`connecting`で最初の有効epochを
  採用するときだけ説明用activityとして使う。**順序保証外**・attempt帰属保証外（best-effort）であり、役割と
  内容は変更しない（2026-07-18 合意: List は劣化体験補助として現状維持）。
- **Frontend reducer**: attempt 境界の判定と、全 transport（SSE / poll）共通の単調 stage merge。

## Work Definition

### Problem

stage は DB 書込と Redis Stream publish へ独立に fan-out され、両方 best-effort である。
このため「DB 書込は成功、Stream publish だけ失敗」が設計上起こり得る。

現在の frontend 合成規則は、一度でも SSE event を受理すると `live` / `reconnecting` 中の
polling progress を無条件に無視する。その結果、上記の片側成功時に DB では工程が進んで
いるのに、表示が古い stage（または「生成中」）のまま run 終了まで止まる。

さらに、DB 値の attempt 帰属が現状保証されていない。

- 再取得（reacquire）は attempt_epoch を増やすだけで progress_stage を残すため、
  新 attempt の行に旧 attempt の stage が残留する。
- progress writer / worker-owned terminal 書込（complete_run / mark_failed）に
  attempt_epoch fence がなく、旧 attempt の遅延書込・ゾンビ worker の terminal が
  新 attempt の行へ反映され得る。

また、新 attempt の最初の SSE event が stage 以外（activity 等）の場合、reducer の
attempt 初期化で progressStage が null へ戻り、polling では回復できない。

### Evidence

2026-07-18 時点の実装を根拠とする。

- `backend/app/agent/live_updates/reporters.py:63` — stage / activity の fan-out は
  `asyncio.gather(..., return_exceptions=True)` で DB 書込と Stream publish を独立実行する。
- `backend/app/agent/runs/repository.py:164-177` — `acquire_for_execution()` の UPDATE は
  `status / started_at / attempt_epoch + 1` のみで、`progress_stage` をリセットしない。
  再取得後の行は「新 epoch + 旧 attempt の stage」になる。
- `backend/app/agent/runs/progress.py:27-46` — `AgentRunProgressWriter` の UPDATE は
  `run_id + status = RUNNING` のみで fence し、attempt_epoch 条件を持たない。
- `backend/app/agent/runs/repository.py:246-260` — `complete_run()` は
  `status = RUNNING` のみで fence。`repository.py:100-121` — `mark_failed()` は
  `status IN active` のみで fence。いずれも attempt_epoch 条件を持たず、
  ゾンビ worker が再取得後の run を completed / failed にできる。
- terminal 書込の呼び出し元: worker-owned は `app/queue/tasks/agent_run.py`
  （complete_run / mark_failed）のみ。`mark_enqueue_failed()`（queued 限定）と
  `sweep_stale_runs()`（run 単位で stale を殺す）は attempt に紐づかない run 単位の操作。
- `backend/app/models/agent_run.py:114` — `attempt_epoch` は default 0・`ge 0`。
  **0 は「未取得 queued」の正常値**であり不正値ではない。
- `backend/app/schemas/research.py:116` — `ResearchRunResponse` は attemptEpoch を持たない。
- `backend/app/agent/runs/repository.py:264` — `read_run_for_user()` は `AgentRun` 行全体を
  select して `build_research_run_response()`（`runs/projection.py:26`）へ渡すため、
  field 追加はクエリ変更なしで可能。
- `backend/app/agent/live_updates/recent_events.py:47-48` — List entry の payload は
  event 本体 + ts のみで attemptEpoch を持たない。`app/queue/tasks/agent_run.py:97` の
  `events.reset()` は best-effort（失敗を握る）。List の attempt 帰属は保証できない。
- `frontend/src/features/research/live/controller.ts:322` — `shouldApplyPollingProgress()` が
  「polling-only、または connecting で SSE 未受理」のときだけ poll progress を適用する。
- `frontend/src/features/research/live/controller.ts:293` — terminal（completed / failed）は
  接続モード非依存で DB を正本として適用する（本 slice で維持）。
- `frontend/src/features/research/live/reducer.ts:73` — SSE stage event は**無条件代入**。
  poll は `lastProcessedEventId` を進めないため、poll 適用後に遅延到着した古い SSE stage が
  dedup を通過して表示を巻き戻せる。
- `frontend/src/features/research/live/controller.ts:341` — hidden 中は poll 停止、
  失敗時は指数 backoff。「N 秒以内に回復」は無条件には保証できない。
- `frontend/src/features/research/live/controller.test.ts:521` — 「SSE 受理後は poll progress を
  無視する」現行規則をテストが固定している（本 slice で置換対象）。

### Invariants

1. 表示 progressStage は同一 attempt 内で後退しない。**SSE / poll のどちらの transport も
   同じ単調 merge 判定を通る**（poll 専用の前進判定にしない）。
2. attempt を跨ぐ stage の大小比較を行わない。attempt の新旧判定は attemptEpoch のみで行う。
3. terminal（completed / failed）は DB を正本とし、接続モード・epoch の有無に依存せず
   最優先で適用する（現行維持）。
4. DB の progress_stage と worker-owned terminal（completed / failed / error_code /
   assistant_message_id）は、その行の `attempt_epoch` が指す attempt に帰属する。
   旧 attempt の遅延書込・ゾンビ worker の terminal は行へ反映されない
   （acquire の stage リセット + writer / terminal の epoch fence で構造的に保証）。
5. 新 attempt の取得時、`progress_stage` は epoch 増分と**同一 UPDATE** で NULL へ戻る。
6. runStatus は `queued < running` の単調 merge とし、遅延 response で demote しない。
   completed / failed は Invariant 3 の terminal 経路が処理する。
7. 厳密な attempt 帰属保証の対象は **progressStage / runStatus / terminal に限る**。
   activity（`recentEvents`）は best-effort・順序保証外であり、attempt 帰属を保証しない。
8. draft（answer.delta）は polling から復元しない（現行維持）。
9. API 変更は additive のみとし、既存 field の型・意味を変えない。
10. attemptEpoch は 0 以上の整数（0 = 未取得 queued の正常値）。stage の attempt-aware merge は
    **正の safe integer の epoch を持つ response のみ**を対象とし、epoch が欠落・0・不正な
    response では stage / activity を merge しない（status 単調 merge と terminal のみ適用）。
11. 表示へ反映しない古い SSE event でも `lastProcessedEventId`（Stream ID）は前進させる
    （dedup / cursor 契約の維持）。
12. 進捗配信・terminal fence の失敗で run を落とさない。fence により更新 0 行となった
    stale worker の terminal 書込は、既存の transition-lost 意味論（complete_run の
    `RunTransitionLostError` / mark_failed の False 返却）で敗北として扱う。

### Non-goals

- Redis List への stage / terminal / delta の複製（2026-07-18 議論で不採用）。
- List entry / key の attemptEpoch fence 化（Invariant 7 で activity を保証対象外としたため）。
- stage の Stream publish への retry 追加や Stream 配信の信頼化。
- `live` / `reconnecting` モードでの `recentEvents`（List activity）合成
  （polling-only 限定の現行規則を維持）。
- `mark_enqueue_failed()` / `sweep_stale_runs()` への epoch fence 追加
  （attempt に紐づかない run 単位の操作であり、意味論が異なる）。
- run polling 以外の endpoint（thread detail 等）への attemptEpoch 追加。
- SSE の再接続 cursor / replay 契約の変更。
- reducer の `resetForAttempt` が stage を null 化する挙動の変更
  （次回成功 poll の前進 merge が回復させるため、reducer 側は触らない）。

### Done

- 「DB stage 書込成功 + Stream stage publish 失敗」の再現テストで、SSE 受理後の状態でも
  次回成功 poll で表示 stage が前進する。
- 「poll 適用後の遅延 SSE stage」の再現テストで、表示が巻き戻らず Stream ID は前進する。
- reacquire の再現テストで、旧 attempt の stage 残留・stale worker の terminal 反映が
  構造的に起きない。
- 合成規則（同一 epoch 後退無視 / 前進反映 / 新 epoch reset 採用 / epoch 欠落・0・不正時の
  非 merge / 遅延 queued の非 demote）がテストで固定される。
- `/gen-types` 実行済み、`/check` green。

## 設計

### Backend（4 点）

1. **`ResearchRunResponse` に `attempt_epoch: int = Field(ge=0)` を追加**
   （camelCase: `attemptEpoch`）。`read_run_for_user()` は行全体を読んでいるため
   `build_research_run_response()` への追加のみ。DB 変更なし・migration 不要。
2. **`acquire_for_execution()` の同一 UPDATE で `progress_stage = NULL`**。
   epoch 増分と stage リセットを原子的に行い、新 attempt の行に旧 stage を残さない
   （Invariant 5）。
3. **`AgentRunProgressWriter` の epoch fence**。constructor で attempt_epoch を受け、
   UPDATE の WHERE へ `AgentRun.attempt_epoch == <writer の epoch>` を追加する
   （`status = RUNNING` 条件は維持）。組み立て箇所は
   `app/queue/tasks/agent_run.py` で `prepared.attempt_epoch` を注入する。
4. **worker-owned terminal の epoch fence**。`complete_run()` / `mark_failed()` に
   `expected_attempt_epoch` を追加し、WHERE へ `AgentRun.attempt_epoch == expected_attempt_epoch`
   を加える。worker（`agent_run.py`）が `prepared.attempt_epoch` を渡す。
   更新 0 行時の挙動は既存契約を維持する（complete_run: `RunTransitionLostError`、
   mark_failed: False）。`mark_enqueue_failed()` / `sweep_stale_runs()` は対象外（Non-goals）。

### Frontend 合成規則（rule 36 の置換）

poll response への適用は次の**順序付き 3 段**とする。

1. **terminal**: completed / failed は epoch の有無・値を問わず最初に適用する（現行維持）。
2. **active status**: `queued < running` の単調 merge。running response で queued → running へ
   前進させ、遅延した queued response では demote しない。
3. **stage / activity**: response の attemptEpoch が**正の safe integer**
   （`Number.isSafeInteger(e) && e >= 1`）の場合のみ、次の attempt-aware merge を行う。
   それ以外（欠落・null・0・負数・小数・string・unsafe integer）は merge しない。

attempt-aware merge（`pollEpoch` = response、`curEpoch` = `liveState.currentAttemptEpoch`）:

- **curEpoch === null**（attempt 未観測）: pollEpoch を採用し、stage を適用。activity は
  現行の表示規則（`latestRelevantPollingActivity`）で適用する。
- **pollEpoch < curEpoch**: stage / activity とも無視（stale response）。
- **pollEpoch === curEpoch**: stage は `null < planning < retrieving < synthesizing` の順で
  **厳密に前進する場合のみ**反映。activity は polling-only モードのときのみ現行規則で反映。
- **pollEpoch > curEpoch**: `attempt.started` 受理と同等の attempt reset
  （stage / activity / draft を初期化し epoch を採用）を行い、poll の stage を適用する。
  **この response の activity は適用しない**（List の attempt 帰属は保証外のため。
  次回 poll または SSE に委ねる）。以後、旧 epoch の SSE event は reducer の既存 fence が排除する。

### Reducer 共有の単調 stage merge

stage の前進判定は reducer module の純関数（例: `advanceStage(current, next)`）とし、
**SSE `stage` event の適用と poll merge の両方が同じ関数を通る**（Invariant 1）。

- SSE stage event が現在の表示 stage より古い場合、表示 stage は変えない。
  ただし `lastProcessedEventId` は前進させる（Invariant 11）。
- attempt 境界（SSE の新 epoch event / poll の新 epoch）では stage null から再開する。
  これは後退ではなく attempt 初期化である。

epoch 比較を含む merge 判定は reducer module が所有する（`currentAttemptEpoch` の遷移規則
という同じ不変条件を controller と 2 箇所へ分散させない）。

### 順序安全性の根拠

同一 attempt 内の worker の stage 遷移は単調
（planning → retrieving → synthesizing。direct 経路は retrieving を跳ぶが後退しない）。
これを破る 3 つの反例は次で遮断する。

- 旧 attempt の stage 残留 → acquire の同一 UPDATE リセット（設計 2）。
- 旧 attempt の遅延 DB 書込 → writer / terminal の epoch fence（設計 3・4）。
- 遅延 SSE stage の巻き戻し → reducer 共有の単調 merge（上記）。

### 回復時間の表現

「画面表示中かつ通信正常なら、次回成功 poll（通常約 2 秒）以内に表示 stage が回復する」。
hidden 中は poll が停止し、失敗時は指数 backoff により遅延するため、無条件の上限は約束しない。

### 互換性 / デプロイ順序

frontend の attempt-aware merge を有効化する前に、attemptEpoch を返す backend response と DB の
epoch fence を全 backend instance へ deploy し、旧 unfenced worker を drain する。これにより、新規
frontend が読む DB 値の attempt 帰属を保証する。frontend 先行 deploy では epoch のない response が
stage / activity を merge しないため、poll による進捗回復を一時的に失う。これは現行同等とは表現しない。

## 変更ファイル（想定）

backend:

- `app/schemas/research.py` — `ResearchRunResponse.attempt_epoch`（`ge=0`）
- `app/agent/runs/projection.py` — builder へ field 追加
- `app/agent/runs/repository.py` — acquire の stage リセット / complete_run / mark_failed の
  epoch fence
- `app/agent/runs/progress.py` — writer の epoch fence
- `app/queue/tasks/agent_run.py` — writer / terminal への attempt_epoch 注入
- 対応する契約テスト

frontend:

- `src/types/*.gen.ts` — `/gen-types` で再生成
- `src/features/research/live/reducer.ts` — 共有単調 merge 純関数 + poll merge
- `src/features/research/live/controller.ts` — parsePollRun の attemptEpoch 対応 +
  合成規則の 3 段化
- `src/features/research/live/controller.test.ts` / `reducer.test.ts` — 下記テスト計画

## テスト計画

Red-first: backend / frontend それぞれの再現テスト（下記 backend 1・4、frontend 1・2）を
先行させ、現行実装で fail することを確認してから修正を入れる。

backend:

1. epoch 1 + synthesizing の run を reacquire すると epoch 2 + progress_stage NULL になる
   （再現・新規）。
2. 旧 attempt epoch を持つ writer の `stage_changed()` が行を更新しない。
3. 同一 epoch + RUNNING で更新成功、status が RUNNING 以外で no-op（既存維持の確認）。
4. stale worker（旧 epoch）の `complete_run()` が更新 0 行で `RunTransitionLostError` となり、
   新 attempt の run を completed にしない（再現・新規）。
5. stale worker の `mark_failed()` が False を返し status を変えない。
6. `GET /runs/{run_id}` が行の `attempt_epoch` を `attemptEpoch`（`ge=0`）として返す。
   queued run では 0 を返す。
7. `sweep_stale_runs()` / `mark_enqueue_failed()` が epoch に依存せず従来どおり動作する
   （fence 対象外の確認）。

frontend:

1. SSE 受理後（live 相当）に stage event が欠落し、poll が同一 epoch の先行 stage を
   返したとき表示が前進する（再現・新規）。
2. poll で synthesizing を適用した後、遅延 SSE の retrieving（同一 epoch）を無視し、
   `lastProcessedEventId` は前進する（再現・新規）。
3. SSE で synthesizing を表示した後、poll の retrieving（同一 epoch）を無視する
   （`controller.test.ts:521` 系の既存テストを本規則へ置換）。
4. poll が新 epoch を返したとき attempt reset + stage 採用が起き、**同じ response の
   activity は適用されず**、以後の旧 epoch SSE event が無視される。
5. 遅延した queued / epoch 0 の poll response が running 表示・表示中 stage を戻さない。
6. attemptEpoch が欠落 / null / 負数 / 小数 / string / unsafe integer の各 parse 経路で
   stage / activity merge が行われない（status 単調 merge と terminal は機能する）。
7. terminal は epoch が不正でも収束する。
8. 新 attempt の最初の SSE event が activity のとき、次回成功 poll で stage が回復する。

## Verification

`/check`（backend + frontend）と上記テスト。SSE 片側失敗・ゾンビ worker の実環境注入は
困難なため、reducer / writer / repository のユニットテストを正とし、手動確認は通常経路
（SSE 正常時の表示遷移と terminal 収束）の非退行のみとする。

## 未決事項

なし。`frontend/specs/agent-research-live-ui-slice.md` の rule 36 本文は本 slice の
合成規則に合わせて改訂済み。
