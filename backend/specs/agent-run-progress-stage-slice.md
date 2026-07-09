# Agent run progress_stage 表示 slice 仕様 (Slice 4)

## 位置付け

親仕様: `specs/agent/conversation-history-async-runs.md`。前提 slice: Slice 1〜3 +
引用リンク化（実装済み前提）。本 slice は **run 実行中の「今どの作業単位にいるか」
（荒い分類 = 状態）の可視化**を作る。

Slice 5 との棲み分け（合意済みの分類基準）:

- **Slice 4 = 状態（DB）**: planning → retrieving → synthesizing の 3 値。
  ①リロード後も復元価値がある ②書き込みは 1 run 最大 3 回と安い
  ③終状態後も「どの工程で死んだか」の診断価値が残る → DB が正しい置き場。
- **Slice 5 = イベント（Redis）**: 「Tavily で ◯◯ を検索中」等の細かい分類。
  復元価値がなく高頻度 → Redis capped list + TTL、polling 応答に recentEvents 同乗。
  本 slice ではイベント語彙・Redis 配管を**先取りしない**（seam は本 slice が
  必要とする形だけ切る）。

**DB 変更なし**: `agent_runs.progress_stage` 列と CHECK
（`planning/retrieving/synthesizing` 3 値、`ck_agent_runs_progress_stage`）は
Slice 1 で作成済み。`/migration` 不要。

## Problem

run 実行中のユーザーには「生成中」としか見えず、30 秒級の待ち時間中に何が
起きているか分からない。worker が工程境界で progress_stage を更新し、
polling 応答と thread 詳細に載せ、実行中スピナーの文言を工程に応じて
切り替えられるようにする。

## Evidence（調査済みの既存構造）

- **工程境界は `QuestionAnsweringService.answer()` に集約済み**
  （`app/agent/answering/service.py:66-90`）: `planner.plan()` →
  `NoRetrievalPlan` なら `direct_answerer.answer()`（direct 経路）/
  evidence 系 plan なら `_answer_with_evidence()` = `collect()` → `synthesize()`。
  → seam はこの service に切れば全経路を覆える。
- **agent は Pure DI**: `composition.build_question_answering_agent()` が
  worker task から per-task に組み立てる（Slice 2 設計判断 3）。constructor 注入が
  既存流儀。
- **acquire_for_execution** が queued/running → running の条件付き UPDATE を持つ
  （`history/repository.py`）。再配送復旧時も同経路で running に戻る。
- **session/pool（現状は進捗書き込みと競合し得る）**: worker は `agent_session` を
  `agent.answer()` 全体で保持し、internal retrieval はその session で query を実行する
  （`article_search.py:124`、commit/rollback なし）。SQLAlchemy の autobegin により
  **最初の読み取りから session close（= answer() 終端）まで connection が保持される** —
  つまり internal/mixed run では LLM 合成待ちの間も connection が塞がる。
  agent label の pool は (5,5)=cap 10、`--max-async-tasks 10` のため、internal 系 run が
  並ぶと進捗 writer の別 session が checkout 待ちに陥り stage 更新が系統的に
  欠落し得る。→ 前提整理 1 で読み取り tx の衛生を先に回収する
  （Slice 2 invariant「LLM 待ち中に tx を保持しない」の read tx 版。既存の潜在問題を
  本 slice が顕在化させた形）。回収後は connection 需要が瞬間的になり cap 内に収まる。
  それでも起きる瞬間的な枯渇は best-effort 失敗として吸収（設計判断 4）。
- **pre-answer failure の存在**: worker は `build_question_answering_agent()` 内の
  構成チェック（`composition.py:25,30`）で `answer()` に入る前に失敗し得る
  （→ generation_unavailable）。この経路では progress は一度も報告されない。
- **polling 中継**: frontend Route Handler は backend の slim run response を
  そのまま中継するため、フィールド追加は /gen-types 以外の変更不要。
- **1 thread 1 active run**（partial unique index）→ 表示すべき進捗は
  thread あたり常に高々 1 つ。

## 前提整理（既存 tx 衛生の回収、本 slice 冒頭で実施）

1. **internal retrieval の読み取り tx を読了ごとに閉じる**。現状は autobegin の
   read tx が `answer()` 終端まで connection を保持する（Evidence 参照）。
   検索の読み取りを明示 tx（`async with session.begin():` 相当）で包み、
   読了時に connection を pool へ返す。検索間に cross-query 一貫性の要求は無く、
   read-only commit のコストは無視できる。これにより「LLM 待ち中に tx /
   connection を保持しない」が read にも成立し、進捗 writer との pool 競合が
   構造的に解消される。配置（article_search / evidence_collection のどちらの層で
   閉じるか）は実装時に確定。

## 設計判断

1. **seam = `AnswerProgressReporter` Protocol を agent core に constructor 注入**。
   語彙 `AnswerProgressStage = Literal["planning", "retrieving", "synthesizing"]` と
   Protocol（`async def stage_changed(stage) -> None`）は `app/agent/contract.py` に
   置く（agent core が自工程を自己申告する語彙。DB CHECK の 3 値と一致）。
   `QuestionAnsweringService` は `progress: AnswerProgressReporter | None = None` を
   受け、None なら no-op（router の構成チェック・既存テストは無影響）。
   Slice 5 はこの同じ通知経路に sink を足す（agent core を再び触らない）。
2. **工程割り付けは service が全段を報告する**（acquire に progress を混ぜない —
   run 状態機械と進捗表示の関心を分離し、stage 意味論の所有者を agent core
   一箇所にする）:
   - `answer()` 冒頭（`planner.plan()` 前）→ `planning`
   - evidence 経路: `collect()` 前 → `retrieving`、`synthesize()` 前 → `synthesizing`
   - direct 経路: `direct_answerer.answer()` 前 → `synthesizing`
     （**retrieving は skip** — 実際に検索しないため。planning → synthesizing と遷移）
   - 再配送復旧の再実行でも `answer()` が再走するため planning から正しく再報告される。
   - **pre-answer failure（agent 構築・構成チェックでの失敗）は progressStage null を
     許容する**。工程の外で死んだ run に工程を捏造しない — null 自体が
     「どの工程にも入らず失敗した」という診断情報になる。worker が build 前に
     planning を書く案は、stage 意味論の所有者を 2 箇所にするため不採用。
3. **書き込みは worker 側 reporter が所有する**: `app/agent/history/progress.py`
   （新規）に `AgentRunProgressWriter(session_factory, run_id)`。報告ごとに
   短命 session + 単文 tx で
   `UPDATE agent_runs SET progress_stage = :stage WHERE id = :run_id AND status = 'running'`
   （条件付き UPDATE — 終状態・sweeper 敗北後の run を蘇生しない。rowcount 0 は
   静かに no-op）。メトリクス/永続化は durability boundary の所有者が出す、の
   既存方針どおり worker 側実装に閉じ、agent core は DB を知らない。
4. **進捗は装飾 — 失敗で run を落とさない（best-effort）**。writer は全例外を
   捕捉して warning log（run_id + stage のみ、PII-free。本文・例外文言を焼かない）。
   pool checkout timeout も同様に吸収。進捗の欠落は許容し、回答の成否に影響させない。
5. **契約: `progressStage` を polling 応答と詳細契約の両方に追加**
   （`ResearchRunResponse` / `ResearchMessageRun` に
   `Literal["planning","retrieving","synthesizing"] | None`）。詳細側に載せる理由 =
   refresh 直後・ページ再訪時に最初の poll を待たず正しい工程から表示を開始できる。
   projection は status/errorCode と同じ StrEnum parse 方式で写像。
6. **終状態後も値は残す**（completion/failure の UPDATE は progress_stage を
   触らない）。failed run の「どこで死んだか」が読める。表示に使うのは
   running のときだけ（frontend 責務）。
7. **frontend: polling コンポーネントを active user message の status 行へ移設**。
   現在の `ResearchRunPoller`（section 先頭で null 描画・信号専用）を、
   active な user message の status 行に置く `ActiveRunStatus`（client）へ再編し、
   **polling と stage 文言表示を同一コンポーネントの local state に閉じる**。
   1 thread 1 active run の構造保証により polling ループは引き続き単一。
   - 文言は **status × progressStage の両方で決まる**: queued=「待機中」/
     running × planning=「計画中」/ retrieving=「情報収集中」/
     synthesizing=「回答作成中」/ running × null（pre-answer 過渡・欠落時）=
     「生成中」fallback。
   - props は `runId` + **`initialStatus`** + `initialStage`（詳細契約の
     `run.status` / `run.progressStage`）。初回 poll 前もこの初期値で正しく分岐し、
     以後は polling 応答の `status + progressStage` を local state に持って更新する。
   - **stage 変化で `router.refresh()` しない**（terminal 検知時のみ refresh、
     既存の停止規律 — visibility/バックオフ/404 — はそのまま移設）。
   - 「polling は信号専用」の定義を「run 信号 = status + progressStage の
     ephemeral 表示。会話データの描画は詳細一本」に更新する（Slice 3 で
     予告済みの限定緩和）。

## API Contract（/api-contract + /gen-types）

```text
GET /api/v1/research/runs/{runId}
  200: { runId, threadId, status, errorCode,
         progressStage: "planning" | "retrieving" | "synthesizing" | null }  # 追加

GET /api/v1/research/threads/{threadId}
  messages[].run（user message）に progressStage を同追加
```

他 endpoint・error_code 語彙・status 語彙は不変。frontend Route Handler は
中継のみで変更なし（生成型の再生成のみ）。

## 実行フロー

```text
worker run_agent_answer:
  acquire（running 遷移、従来どおり）
  → AgentRunProgressWriter(session_factory, run_id) を生成し
    build_question_answering_agent(..., progress=writer) に注入
  → answer() 内: planning 報告 → plan
      direct 経路:   synthesizing 報告 → direct answer
      evidence 経路: retrieving 報告 → collect → synthesizing 報告 → synthesize
    （各報告 = 短命 session の条件付き UPDATE、失敗は warning log のみ）
  → 完了/失敗 tx は従来どおり（progress_stage は触らない）

frontend:
  詳細描画 → active user message の status 行に
  ActiveRunStatus(runId, initialStatus, initialStage)
  → 2s polling（既存規律のまま）→ status + progressStage を local 更新して文言決定
  → terminal 検知 → router.refresh() → active が消え自然停止（従来どおり）
```

## New Types / Structure

```text
backend/app/agent/internal_retrieval/ or evidence_collection/
                                         (前提整理 1: 読み取り tx を読了ごとに閉じる)
backend/app/agent/contract.py            (AnswerProgressStage + AnswerProgressReporter)
backend/app/agent/answering/service.py  (progress 注入 + 3 箇所の報告)
backend/app/agent/composition.py        (progress 引数を worker から受けて配線)
backend/app/agent/history/progress.py   (新規: AgentRunProgressWriter — 条件付き UPDATE、best-effort)
backend/app/agent/history/projection.py (progressStage 写像追加)
backend/app/queue/tasks/agent_run.py    (writer 生成 + 注入)
backend/app/schemas/research.py         (ResearchProgressStage Literal、2 schema に追加)
frontend/src/features/research/components/
  ActiveRunStatus.tsx                    (新規: ResearchRunPoller の polling 規律を移設 + stage 表示)
  ResearchThreadView.tsx                 (UserMessage の active 状態表示を ActiveRunStatus へ)
frontend/src/types/*.gen.ts              (/gen-types 再生成)
```

## Invariants

- 進捗書き込みは `status = 'running'` への条件付き UPDATE のみ。終状態は不変で、
  進捗が run の状態機械（queued/running/completed/failed）を動かすことはない。
- 進捗の書き込み失敗・欠落は run の成否に影響しない（best-effort）。
- reporter は LLM 待ちを跨いで tx / connection を保持しない（報告ごとに短命 tx）。
- agent_session も読み取り tx を LLM 待ちを跨いで保持しない（前提整理 1 で成立。
  write に限っていた Slice 2 invariant を read へ拡張）。
- agent core は DB・run_id を知らない（Protocol 越しの自工程申告のみ）。
- 会話データの描画は thread 詳細一本のまま（polling から描画するのは
  status + progressStage の ephemeral 表示のみ）。
- log に質問・回答本文を載せない（run_id + stage のみ）。

## Non-goals

- 細粒度イベント（検索クエリ等）の発火・Redis 配管・recentEvents（Slice 5）。
- progress_stage の語彙拡張（3 値は親仕様合意。粒度が足りない分は Slice 5 の領分）。
- polling 間隔の変更（2s のまま。3 段階の粒度に十分）。
- 進捗の pipeline_events 監査焼き込み（consumer 不在）。
- 過去 run の progress_stage 表示（terminal では表示しない。値は診断用に残るのみ）。

## Tests

backend:

1. service（stub reporter）: evidence 経路で planning → retrieving → synthesizing の
   順に報告される / direct 経路で planning → synthesizing（retrieving なし）/
   reporter None（no-op）で従来どおり動く。
2. writer: running の run に stage が書かれる / completed・failed には書かれない
   （rowcount 0 no-op、値が不変）/ 書き込み例外（DB 断 fake）が握り潰され
   warning log のみで raise しない。
3. worker 統合: fake agent で run 完走後も progress_stage が最後の報告値のまま残る
   （完了 UPDATE が触らないこと）/ `answer()` 内で死んだ run に死亡地点の stage が
   残る / **pre-answer failure（build 失敗）は progressStage null のまま failed になる**。
   3b. 前提整理 1: internal retrieval の読了後に session が tx を持たない
   （`session.in_transaction()` が false / connection が返却される）。
4. projection / API: progressStage が polling 応答と詳細の run に載る（null 含む）/
   OpenAPI 生成型に届く（/gen-types）。

frontend:

5. ActiveRunStatus: status × stage の文言決定 / **初回 poll 前の queued（待機中）**/
   **初回 poll 前の running + null（生成中 fallback）**/ 初期値が
   props（initialStatus + initialStage、詳細契約由来）から出る /
   stage 変化で refresh されない / terminal で refresh + 停止・visibility・
   バックオフ・404 の既存規律が移設後も保たれる
   （ResearchRunPoller の既存テストを移行）。

## 検証の制約

- dev は egress 制約で実 LLM 不可のため、stage 遷移の実観測は
  unit（stub/fake）中心。dev compose の構成エラー経路は **pre-answer failure なので
  progressStage null のまま failed になることの確認**（UI は「生成中」fallback →
  失敗表示）。全 3 段の実観測は本番 deploy 後。
- `/check` + `/gen-types`。`/migration` 不要（Evidence 参照）。

## Done

- 実行中の user message に「計画中 → 情報収集中 → 回答作成中」が polling で
  live 切替表示される（direct 経路は 計画中 → 回答作成中）。
- リロード・再訪時も詳細契約の progressStage から正しい工程で表示が始まる。
- 進捗書き込みの失敗が run を落とさず、終状態・冪等・sweeper の既存不変条件が
  すべて保たれる（既存 suite green）。
- 新規テスト green + /gen-types 済み。
