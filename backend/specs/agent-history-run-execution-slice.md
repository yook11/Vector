# Agent 非同期 run 実行・保存経路 slice 仕様 (Slice 2)

## 位置付け

親仕様: `specs/agent/conversation-history-async-runs.md`。前提 slice: Slice 1（schema、
実装中）+ source 表示契約変更（`agent-source-display-contract-slice.md`）。
本 slice は **POST の 202 化 / taskiq worker での agent 実行 / 会話の保存経路 /
GET runs polling（status のみ）/ stale sweeper** を作る。thread 一覧・詳細・DELETE・
frontend 結線は Slice 3、progress_stage は Slice 4、Redis ライブイベントは Slice 5。

API 変更は破壊的（202 化）だが、フロント未結線のため in-place で行う（親仕様合意済み）。

## Problem

agent 実行が同期 POST の request 内で行われ、履歴も実行状態も残らない。
実行を worker に移し、質問投稿 → 202 → polling → 完了/失敗の全状態が
DB（SSoT）から導出できるようにする。

## Evidence (調査済みの既存規約)

- **broker**: `app/queue/brokers.py:39-85` — `_make_broker(queue_name)` で 7 broker。
  `RedisStreamBroker(idle_timeout=600_000, maxlen=10_000)` + result backend。
  Redis は単一 `settings.redis_url`、ACL は URL 埋め込み（core user は全権 →
  core 専用 queue の追加に infra/redis の ACL 変更は不要）。
- **二重実行前提**: worker は `--ack-type when_executed`（実行後 ack）+
  `SimpleRetryMiddleware(default_retry_count=0)`。crash / idle_timeout(600s) 経過で
  **stream 再 claim = 再配送が起きうる**。既存 task は DB 最新状態からの再構築 +
  `ALREADY_*` 冪等 skip で防御（`app/queue/tasks/assessment.py:52-75`）。
- **task 定義**: `@broker_X.task(task_name=..., timeout=..., max_retries=..., retry_on_error=...)`。
  DTO は `BaseModel(frozen=True)` 必須（taskiq formatter が Pydantic 依存、
  `app/queue/messages/assessment.py:3-6`）。DTO は ID だけ運ぶ軽量 carrier。
- **session**: `ctx.state.session_factory`（`app/queue/lifecycle.py:107-123` の WORKER_STARTUP で
  生成、label 別 pool sizing `WORKER_POOL_SIZING`）。tx は `async with session_factory() as session` +
  明示 commit。
- **AI 配線**: `app/queue/composition.py` — WORKER_STARTUP hook 内で lazy import して
  state 注入。ただし agent は per-task の session を repository に束ねるため、
  組み立て済み service の state 注入は不可（設計判断 3）。
  既存の組み立て実装 = `app/agent/router.py:47-116` の `_build_question_answering_agent`。
- **cron**: schedule 式は `app/queue/schedule.py` に定数化（UTC/JST 時刻表 docstring で
  minute 衝突検証）。task 側 `schedule=[{"cron": ...}]` + `registry.py` 副作用 import +
  `schedulers.py` に TaskiqScheduler。cron task は `max_retries=0, retry_on_error=False` が前例。
- **process group**: `fly.core.toml [processes]` + `[[vm]]`（memory/cpus/processes）+
  `supervisord/*.conf` + ルート `docker-compose.yml` service（`x-worker-base` anchor）。
  worker-analysis=1536mb / insights=768mb の実測 RSS ベース sizing が前例。
- **失敗処理の前例**: AI task は marker 分類（`app/analysis/assessment/errors.py` の
  Retryability 型軸 + 原因 instance 軸）→ task は reraise/hold のみ解釈。
  cron は best-effort audit + raise または握り潰し。
- **API 側の 503 fail-fast**: `app/agent/router.py:92-97` — DeepSeek/Tavily key 欠落は
  構成ミスとして即 503（C-2 設計判断 7、外部検索無効化への縮退はしない）。
- **API からの kiq に broker.startup() は不要**: `app/queue/lifecycle.py:192-216` の
  docstring が正本 — 「API プロセスはそもそも `broker.startup()` を呼ばず `.kiq()` は
  AsyncKicker による lazy 経路。CLIENT_STARTUP は scheduler プロセスでのみ発火する
  (no gate required)」。この前提を壊さないため、API 側に lifespan 配線は**しない**
  （設計判断 4）。broker_agent は cron を持つため `_register_scheduler_lifecycle` +
  schedulers.py への TaskiqScheduler 追加が必要（これは scheduler プロセスの話で API とは独立）。

## 設計判断

1. **taskiq retry は使わない**（`max_retries=0, retry_on_error=False`）。ユーザーが画面で
   待つ対話ワークロードで retry は待ち時間を倍化させるだけ。失敗は即 run failed +
   error_code で返し、リトライは「ユーザーが聞き直す」に一本化する
   （direct 回答の JSON 不正リトライ等、工程内部のリトライは既存のまま）。
2. **再配送は冪等ガードで吸収する**（二重実行前提のため必須）。task 受信時に run を読み:
   - `queued` → 通常実行（running に遷移して開始）
   - `running` → 前回実行が crash した痕跡（task timeout 300s < 再claim 600s のため
     生きた二重実行は構造上起きない）→ **復旧として再実行**（started_at を更新）
   - `completed` / `failed` → 冪等 skip（log のみ。終状態は不変）
3. **agent builder は worker 専用に移す**。202 化後の router は agent を実行しないため、
   `_build_question_answering_agent`（service graph 組み立て + AI adapter の関数内 lazy import）
   は `app/agent/composition.py`（新規）へ移し **worker task だけが per-task に呼ぶ**。
   router に残すのは軽量な構成チェック（key 存在確認 → 503）のみ。API プロセスは
   AI SDK も agent graph も組まない（cold start / memory / lazy import 契約に整合）。
   queue 側 composition.py の state 注入パターンは踏襲しない — agent は per-task session を
   repository に束ねるため組み立て済み instance を state に置けず、責務が一致しない。
4. **enqueue は commit 後、失敗しても受理契約は守る**。POST は「thread 解決 + user_message +
   run(queued) を 1 tx で commit → kiq」。kiq 失敗時は run を
   `failed / error_code='enqueue_failed'` へ更新し、**202 {threadId, runId} を返す**
   （run 行が存在する以上、クライアントには polling で failed を見せるのが SSoT 契約。
   5xx を返すと DB に run が残るのに runId を受け取れず、リロード時に「知らない失敗質問」が
   現れる）。failed 更新も失敗（DB 断）した場合のみ 503 とし、queued 孤児は sweeper が回収する。
   API は `broker.startup()` を**呼ばない** — `.kiq()` は AsyncKicker の lazy 経路が
   文書化済みの前提（Evidence 参照）で、lifespan 配線は CLIENT_STARTUP =
   scheduler 専用の前提を壊すため行わない。
5. **run の状態遷移はすべて条件付き UPDATE**（`WHERE status IN (...)` で許可された
   遷移のみ。affected 0 行 = 競合敗北として結果を破棄し log）。終状態
   （completed/failed）は不変。sweeper が failed に倒した後に worker が完走しても
   completed で上書きしない（遅延回答の復活より状態機械の単純さを取る）。
6. **error_code 語彙**（StrEnum、値だけで原因が読める・PII-free）:
   - `generation_unavailable` — AIProviderError / DirectAnswerInvalidError（同期 503 と同じ分類）
   - `internal_error` — 想定外例外（詳細は Logfire のみ、DB に例外文言を焼かない）
   - `enqueue_failed` — broker 投入失敗
   - `stale` — sweeper による回収
   frontend が文言に写像する（backend は表示文言を持たない）。
7. **POST の fail-fast 構成チェックは維持**。DeepSeek/Tavily key 欠落は run を作らず
   従来どおり 503（C-2 の failure visibility 方針を継承）。worker 側でも
   AIProviderConfigurationError → `generation_unavailable` を backstop で持つ。
8. **同一 thread の直列化は thread 行 lock で行う**。既存 thread への POST は
   `SELECT ... FOR UPDATE` → active run の存在確認 → 409 or（次 seq 採番 + insert）。
   lock 下で検査するため race せず、`uq_agent_runs_thread_active` は backstop。
   新規 thread は commit まで不可視のため lock 不要。worker の完了 tx も同じ lock で
   assistant message の seq を採番する。
9. **thread title は作成 tx 内でサーバー側確定**: `question[:50]`（strip 済み質問の先頭
   50 字。省略記号は付けない — 表示側の責務）。
10. **queue / process 構成**: `broker_agent = _make_broker("agent")`、新 process group
    `worker-agent`（fly.core.toml、768mb 初期値・deploy 後 RSS 実測で右サイズ）、
    `supervisord/agent.conf`、docker-compose service 追加。`WORKER_POOL_SIZING` に
    label `agent` を追加（(5,5) 均一に従う）。
11. **sweeper は broker_agent の cron として同居させる**（10 分周期、minute は
    `3,13,23,33,43,53 * * * *` と**ずらす** — schedule.py の時刻表 docstring は minute 衝突
    確認を運用ルールとしており、`*/10` は 0,10,20,… で既存 cron と重なる。実装時に
    時刻表へ追記して衝突が無いことを確認する）。対象は
    `queued`/`running` のまま **20 分**超過した run（再配送による自己復旧の機会
    = 600s を跨いでから回収する閾値）。DB のみ・AI 不要・冪等（条件付き UPDATE）。
    read 側での stale 補正はしない（親仕様 Invariant 10）。
12. **GET runs polling は rows のみから導出**。completed の result は
    messages/sources 行 → response schema の写像で再構築する。この写像は Slice 3 の
    thread 詳細と共有される read projection として実装する。
13. **read 契約の internal `articleId` は nullable に一本化**。非同期化により全応答が
    DB read になるため、「live は required / 履歴は nullable」の区別は消滅する
    （記事削除が run 完了と polling の間に起きうる）。`ResearchResponse` 系 schema を
    rows 由来の read 契約として作り直し、`ResearchResponse.from_result` は
    worker の保存 mapper（contract → rows）に置き換える。
14. **保存 mapper が app-layer invariant を構造化する**: source は assistant message に
    のみ付く / internal は analyzed_article_id 非 NULL・url NULL / external は url +
    evidence_claim 非空（Slice 1 設計判断 8・14 の申し送りの回収先）。
    `AnswerQuestionResult` → rows の写像は単一ファクトリに閉じる。

## API Contract（/api-contract + /gen-types）

```text
POST /api/v1/research/responses        (認証: get_current_user)
  Request:  { question: str(1..1000, strip), threadId?: uuid }
  202: { threadId: uuid, runId: uuid }
  401 / 422: 既存標準
  404: threadId が不所有・不存在（存在秘匿のため 403 にしない）
  409: thread に active run あり (detail: "A run is already in progress for this thread")
  503: 構成ミス fail-fast（既存文言のまま）

GET /api/v1/research/runs/{runId}      (認証: get_current_user)
  200: {
    runId: uuid,
    threadId: uuid,
    status: "queued" | "running" | "completed" | "failed",
    result: { answer, sources, missingAspects } | null,   # completed のみ非 null
    errorCode: str | null                                  # failed のみ非 null
  }
  404: 不所有・不存在
  # sources の internal variant は articleId: int | null（設計判断 13）
  # external variant は evidenceClaim（source 表示契約変更 slice の形）
```

polling は 2 秒間隔（frontend 実装は Slice 3）。

## 実行フロー

```text
POST: auth → (threadId あり: thread を FOR UPDATE + 所有権確認 + active run 検査
             / なし: thread 新規作成 title=question[:50])
      → user_message(次 seq) insert + thread.updated_at bump + run(queued) insert → commit
        （bump は「message 追加時」の親仕様どおり。active run 中・失敗 run の thread も
         ユーザーの最終活動として一覧の先頭に浮く）
      → kiq AgentRunTrigger(run_id) → 202 {threadId, runId}
      kiq 失敗: run を failed/enqueue_failed へ更新 → それでも 202 を返す
                （更新も失敗なら 503 + log。queued 孤児は sweeper 回収）

worker task run_agent_answer (broker_agent, timeout=300, max_retries=0):
      run 読取 → 冪等ガード（設計判断 2）→ 条件付き UPDATE running + started_at
      → composition から agent 構築（session + make_safe_async_client）→ answer()
      → 完了 tx（全か無か）: thread FOR UPDATE → assistant_message(次 seq) + sources
        insert(flush) → run を completed + assistant_message_id + completed_at へ
        条件付き UPDATE → **rowcount == 0（sweeper 等に敗北）なら tx 全体を rollback**
        （assistant_message / sources / bump を一切残さず log のみ）
        → rowcount == 1 なら thread.updated_at bump → commit
      → 例外: run failed + error_code + completed_at（条件付き UPDATE、敗北時は何も書かない）

sweeper sweep_stale_agent_runs (broker_agent cron "*/10 * * * *"):
      queued/running かつ 基準時刻(started_at or created_at) が 20 分超過
      → failed / error_code='stale'（条件付き UPDATE、件数を log）
```

## New Types / Structure

```text
backend/app/agent/composition.py            (新規: agent builder を router から移設、worker 専用)
backend/app/agent/router.py                 (202 化、kiq、404/409、GET runs 追加。
                                             builder 削除、軽量 config check のみ残す)
backend/app/agent/conversations/            (保存 mapper + conversation read projection / repository)
backend/app/agent/runs/                     (run lifecycle projection / repository / types)
backend/app/queue/brokers.py                (broker_agent 追加)
backend/app/queue/messages/agent_run.py     (新規: AgentRunTrigger(run_id: UUID))
backend/app/queue/tasks/agent_run.py        (新規: run_agent_answer + sweep_stale_agent_runs)
backend/app/queue/schedule.py               (CRON_AGENT_RUN_SWEEP 追加 + 時刻表更新)
backend/app/queue/schedulers.py / registry.py / lifecycle.py  (agent 配線)
backend/app/schemas/research.py             (202 response / run response / read 契約化)
backend/supervisord/agent.conf              (新規)
backend/fly.core.toml                       ([processes] worker-agent + [[vm]] 768mb)
docker-compose.yml                          (worker-agent service)
```

## Invariants

- 認証必須。run/thread の read/write は必ず user_id で絞る（不一致 404）。
- GET はプロセス内状態を持たず rows のみから導出（親仕様 Invariant 4）。
- run の終状態は不変。すべての状態遷移は条件付き UPDATE。
- **worker の完了 tx は全か無か**: run 遷移の rowcount を確認できない限り
  assistant_message / sources / updated_at bump を commit しない
  （orphan assistant message を作らない）。
- 回答生成中（LLM 待ち）に write tx を開いたまま保持しない。
- AI SDK を API プロセスの import 時にロードしない（既存 lazy import 契約を維持、
  `tests/test_lazy_ai_sdk_import.py` の pin を broker/task import 経路に拡張）。
- 例外の内部文言を error_code / API detail に leak させない（Logfire のみ）。
- テスト目的で認証・制約・冪等ガードを無効化しない。

## Non-goals

- thread 一覧 / 詳細 / DELETE、frontend 結線（Slice 3）。
- progress_stage 更新（Slice 4）・Redis ライブイベント（Slice 5）。
- taskiq retry による自動再試行（設計判断 1。将来必要なら error 分類の
  Retryability 軸を assessment の前例で導入）。
- 過去履歴を agent 入力に使う（Phase 2）。
- pipeline_events への agent run 監査焼き込み（runs テーブル自体が実行記録。
  audit consumer が現れてから）。
- Fly 本番デプロイ・VM 実測（実装後の運用手順。deploy はユーザー操作）。

## Tests

1. POST 202: 新規 thread（title=先頭50字、境界 50/51 字）+ user_message(seq=1) +
   run(queued) が 1 tx で永続化され、kiq が run_id で呼ばれる。
2. POST 既存 thread: seq が末尾+1、**thread.updated_at が bump される**
   （user message も「message 追加」— 親仕様の一覧ソート契約）。
3. POST 409: active run（queued/running）存在時。completed/failed のみなら通る。
4. POST 404: 他人の thread / 不存在 threadId。
5. POST kiq 失敗: run が failed/enqueue_failed になり、**202 {threadId, runId} が返り**、
   直後の GET runs で failed/enqueue_failed が見える。failed 更新も失敗する場合のみ 503。
6. POST 503: key 欠落 fail-fast（run が作られないことも assert）。
7. worker 完了: fake agent の結果から assistant_message + sources（internal/external の
   列写像、ordinal/source_ref）+ run completed + thread.updated_at bump。
8. worker 失敗: AIProviderError → generation_unavailable / 想定外 → internal_error、
   例外文言が error_code に混入しない。
9. 冪等ガード: completed/failed の run への再配送は no-op。running への再配送は再実行。
10. 競合: sweeper が failed にした後の worker 完了 tx は rowcount 0 で**全体 rollback**され、
    assistant_message / sources / updated_at bump が一切永続化されない（orphan 不在を assert）。
11. sweeper: 20 分超過の queued/running のみ failed/stale 化、新しい run は触らない。
12. GET runs: queued/running/completed/failed 各形（result/errorCode の null 規約）、
    404（他人の run）、internal articleId null（記事削除後）が返せる。
13. 保存 mapper: user message への sources 付与を拒否（Slice 1 設計判断 14 の回収）。
14. lazy import: API プロセスの import 経路（app.main → router → queue broker/task）で
    AI SDK が import されない。
15. OpenAPI: 202 化 / run response / articleId nullable が生成型に届く（/gen-types）。

## 検証の制約

- dev は egress/key 制約で実 LLM E2E 不可（既存 slice と同じ）。検証 3 層:
  unit（fake agent で router/task/mapper/sweeper）→ dev compose で worker 起動 +
  queued→failed の実疎通（LLM 不要経路: 構成エラー backstop で failed になることの確認）→
  本番 deploy 後に全経路実確認。
- `/migration` は不要（DB schema 変更なし。Slice 1 の schema を消費するのみ）。
- `/check` + `/gen-types`。

## Done

- 質問 POST → 202 → polling で queued → running → completed/failed が DB のみから返る。
- 会話（成功・失敗とも）が Slice 1 schema に保存され、再起動後も GET で導出できる。
- 再配送・sweeper 競合下でも終状態が壊れない（条件付き UPDATE のテストが green）。
- worker-agent が supervisord / fly.core.toml / docker-compose に配線されている。
- 既存 suite green + /gen-types 済み。
