# Agent run Redis ライブイベント slice 仕様 (Slice 5)

## 位置付け

親仕様: `specs/agent/conversation-history-async-runs.md`。前提 slice: Slice 1〜4
（progress_stage 実装済み前提）。本 slice は **run 実行中の細粒度ライブイベント
（「“NVIDIA Q2 earnings…” など 3 件を検索中」等）を Redis 経由で polling 応答に
同乗させ、stage 文言の下にサブテキスト表示する**。

Slice 4 との棲み分け（合意済みの分類基準）:

- Slice 4 = 状態（DB）: 復元価値あり・低頻度・終状態後も診断価値 → 実装済み。
- **Slice 5 = イベント（Redis）**: 復元価値なし・高頻度・消えても機能は劣化する
  だけで壊れない。DB には一切書かない。

親仕様で**合意済みの土台**（本 slice で再議論しない）:

- 構造化イベント（表示文言でなく型付き payload、日本語文言は frontend 所有）。
- `agent:run:{run_id}:events` に LPUSH + LTRIM（上限 50）+ EXPIRE（15 分）。
  Stream / consumer group は SSE 導入時に置換すればよく、今は過剰。
- publish は fire-and-forget（失敗で run を落とさない）。
- 配信は `GET /runs/{runId}` の `recentEvents` 同乗。SSE は作らない。
- Redis 不読時は `recentEvents: []` で 200（500 にしない。non-authoritative の
  契約宣言であり隠蔽ではない。エラー自体は log に出す）。
- イベント read の前に run 所有権を確認する。

**DB 変更なし・migration 不要。**

## 親仕様の改訂（本 slice で明示的に行う）

提案 2 の「質問本文・回答断片・PII は Redis イベントに入れない」を次に改訂する:

> **元質問の本文・回答断片は入れない。LLM 生成済み検索クエリ
> （external のみ）と、会話 resolver が元質問と異なると判定した自己完結質問
> （`question.resolved.standalone_question`、500 文字以下）は含めてよい。**

根拠（2026-07-09 ユーザー合意 + レビュー反映）:

- recentEvents は run 所有者にのみ返る（user_id 拘束の既存経路に同乗）。
  質問全文・回答本文は既に DB に永続化しており、15 分 TTL の Redis に質問の
  派生物を置くことは既存の露出面より狭いものしか足さない。
- **クエリ文字列を出すのは `external_search.queries_generated` に限定する**。
  external の queries は常に LLM 生成物（`_clean_generated_queries` 通過）だが、
  internal の queries は planner fallback（`safe_fallback_plan` /
  `_clean_plan_queries` 空時の `or [fallback_query]`、
  `planning/contract.py:145,156-162`）で**元質問がそのまま**
  `internal_queries` に入るため、内容を出すと改訂後の禁止事項に衝突する。
  internal は件数のみ。
- external クエリのサイズはドメイン側が既に構造的に cap 済み（Evidence 参照）。
  イベント側に別の件数・文字数制限を作らない（ドメイン規則を複製しない）。
- `question.resolved` は resolver 成功時かつ strip 後の standalone question が元質問と
  異なる場合だけ発火する。元質問そのものを Redis に複製するイベントにはしない。
- 守るべき不変条件は「入れるか」でなく「漏れ先」に置く: publish/read の失敗
  log に event payload を載せない（Invariants 参照）。

## Problem

Slice 4 で「計画中 → 情報収集中 → 回答作成中」の粗い工程は見えるが、最長の
retrieving（並列の internal/external 検索、30 秒級）の内訳が見えない。worker が
検索の節目で構造化イベントを Redis に積み、polling 応答の `recentEvents` として
返し、実行中表示のサブテキストとしてライブ感を出す。

## Evidence（調査済みの既存構造）

- **イベントの発生源は collect() の内側**で、Slice 4 の stage seam
  （`QuestionAnsweringService`）からは見えない:
  - internal: `InternalSearchService.search_articles(queries)`
    （`internal_retrieval/service.py:95`）が queries と最終 hits を知る。
    frozen dataclass、composition で構築。
  - external: `ExternalSearchResearchRunner._search_task`
    （`external_search/runner.py:93`）が task ごとの
    query 生成 → provider 検索（候補 pool）→ evidence 選別の節目を知る。
    外側の `ExternalSearchService` は完了後の outcome しか持たず、
    ライブ発火は runner でしかできない。
- **external クエリはドメイン側が cap 済み、internal は件数のみで文字数 cap 無し**:
  - external: `EXTERNAL_TASK_QUERY_LIMIT = 3` / `EXTERNAL_QUERY_MAX_CHARS = 200`
    （`external_search/contract.py:50-51`、`_clean_generated_queries` が
    runner 側でも strip + truncate + dedupe）→ payload サイズは有界。
    イベント側に新しい数値制限を作らない。
  - internal: `InternalSearchQueries` は `MAX_INTERNAL_QUERIES`（件数）+
    非空検証のみで**文字数 cap を持たない**
    （`internal_retrieval/query_embedding.py:40`）。さらに planner fallback が
    元質問 verbatim を internal_queries に入れる経路がある（親仕様の改訂参照）
    → **internal のクエリ内容はイベントに出さない**（件数のみ。文字数 cap の
    新設より単純に禁止事項と整合する）。
- **Redis client は両プロセスで利用可能**: `app/redis/connection.py` の
  `get_redis()`（遅延シングルトン、API の rate limit で使用実績）。worker
  プロセスでも同関数で接続できる（broker の接続とは独立）。
- **所有権確認済みの read 経路が既にある**: `GET /runs/{run_id}`
  （`router.py:204`）は `read_run_for_user` で user_id 拘束後に 404 収束。
  recentEvents はこの後段に足すだけで新しい authz 面を作らない。
- **taskiq 再配送**で `answer()` が再走し得る（Slice 2）。前回試行のイベントが
  list に残ると復旧 run の表示に過去の残骸が混ざる。
- **frontend polling**: `ActiveRunStatus`（Slice 4）が 2s polling + status ×
  progressStage の文言表示を local state で持つ。Route Handler は中継のみ。
- production Redis は ACL 2 user 構成（Variant B）。新 key pattern
  `agent:run:*` への read/write 権限は**要確認**（検証の制約参照）。

## 設計判断

1. **seam = `AnswerEventReporter` Protocol を新設し、検索サービスへ constructor
   注入する**（Slice 4 の `AnswerProgressReporter` とは別 Protocol）。
   stage の意味論の所有者は `QuestionAnsweringService` 一箇所だったが、
   イベントの所有者は各検索サービス自身であり、混ぜると reporter の実装義務が
   肥大する。語彙（イベント型）と Protocol は `app/agent/contract.py` に置く
   （agent core が自工程を自己申告する語彙。Redis / run_id は知らない）。

   ```python
   class AnswerEventReporter(Protocol):
       async def event_occurred(self, event: AnswerProgressEvent) -> None: ...
   ```

   注入先は `InternalSearchService`（field 追加、None=no-op）と
   `ExternalSearchResearchRunner`（constructor 追加、None=no-op）。
   `composition.build_question_answering_agent` が `events` 引数で受けて配線。

2. **イベント語彙は最小セットで開始する**（発火点の実態に接地。網羅性は
   non-goal、未知 type を frontend が無視する前方互換規約で後から増やせる）:

   | event type | 属性 | 発火点 |
   |---|---|---|
   | `internal_search.started` | `query_count: int` | search_articles 冒頭 |
   | `internal_search.completed` | `hit_count: int` | search_articles 末尾 |
   | `external_search.queries_generated` | `task_index: int, queries: list[str]` | _search_task の query 生成成功直後 |
   | `external_search.candidates_fetched` | `task_index: int, candidate_count: int` | 候補 pool 構築直後 |
   | `external_search.evidence_selected` | `task_index: int, evidence_count: int` | 選別成功直後 |

   - クエリ文字列を運ぶのは `external_search.queries_generated` のみ
     （internal は fallback で元質問 verbatim が混入し得るため件数のみ —
     親仕様の改訂参照）。external queries は `_clean_generated_queries` を
     通過した検証済みの値を**そのまま**運ぶ（イベント側で再 truncate しない）。
   - 失敗系イベントは出さない（表示は stage + terminal 遷移で足りる。
     失敗の記録は task_reports / missingAspects / errorCode の既存責務）。
   - direct 経路はイベントなし（検索しないため。stage のみで表現）。

3. **publisher は worker 側 `app/agent/history/live_events.py`（新規）に置く**。
   `AgentRunLiveEventPublisher(redis, run_id)` が `event_occurred` を実装:
   契約イベントを JSON 化（`type` + 属性 + publisher が押す `ts`）し、
   pipeline で `LPUSH → LTRIM 0 49 → EXPIRE 900` を 1 往復で実行。
   ts は転送メタデータであり publisher が押す（agent core に時計を持たせない）。
   Slice 4 の progress writer と同じく **best-effort**: 全例外を捕捉して
   warning log（`run_id` + `event type` のみ。payload・例外文言を焼かない）。
   - **短い timeout で諦める**: 共有 client（`get_redis()`）は socket timeout
     未設定のため、Redis の遅延・半断で await が長く塞がると検索処理・API 応答が
     Redis 待ちにブロックされ non-authoritative の建前が崩れる。publish / reset /
     read はすべて `asyncio.wait_for`（1 秒級、定数は live_events.py）で打ち切り、
     timeout も他の例外と同じく warning + 続行。cancel 時の redis-py connection
     状態の扱い（破棄されるか）は実装時に `/research` で確認する。

4. **再配送復旧時は acquire 成功後に worker が key を DEL する**
   （`run_agent_answer` 内、publisher の `reset()` 呼び出し。best-effort）。
   前回試行のイベントが復旧 run の表示に混ざる嘘を 1 コマンドで消す。
   repository（DB 境界)には Redis を持ち込まない（escape は task-owned、の
   Slice 3 方針と同じ）。

5. **reader は同 module の `AgentRunLiveEventReader(redis)`**。
   `GET /runs/{run_id}` が所有権確認（既存 `read_run_for_user`）の**後に**
   `LRANGE 0 9`（直近 10 件）を読み、逆順にして古い→新しいで返す。
   - parse は entry ごとに検証し、壊れた/未知 type の entry は**黙って捨てる**
     （deploy 中の新旧 worker 混在に耐える。捨てた事実は debug log で可）。
   - Redis 例外・timeout は捕捉して `recentEvents: []` + warning log
     （run_id のみ）。DB 由来のフィールド（status/progressStage 等）は
     通常どおり返す。
   - router には **`Depends(get_redis_client)`**（`app/dependencies.py:161`）で
     注入する。router/service は `get_redis()` を直接呼ばない既存方針
     （テスト時の dependency_overrides 取っ手）に従う。worker 側の publisher は
     DI container が無いため `app.redis.get_redis()` 直呼びでよい。

6. **契約: `ResearchRunResponse` に `recent_events: list[ResearchRunEvent]` を
   追加する**（polling 応答のみ。`ResearchMessageRun`（thread 詳細）には
   載せない — 復元価値なしの分類どおり。リロード直後は最初の poll まで
   stage のみ表示で許容）。`ResearchRunEvent` は `type` を discriminator とする
   Pydantic discriminated union（event type ごとの schema、`ts` 含む）。
   API SSoT に載せ /gen-types で frontend 型に届ける。
   **前方互換規約**: frontend は未知の event type を黙って無視する（写像の
   default = 非表示）。語彙追加を non-breaking にする。

7. **frontend: `ActiveRunStatus` の stage 文言の下に最新イベント 1 件だけ
   サブテキスト表示する**（ユーザー決定。ミニログは過剰）。
   - 表示条件: `status = running` **かつ** `progressStage = retrieving` の間のみ
     （synthesizing 移行後に検索イベントの残骸を出さない）。
   - 写像（日本語文言は frontend 所有）の例:
     `external_search.queries_generated` → 「“{queries[0]}” など{n}件を検索中」/
     `internal_search.started` → 「関連記事を検索中」/
     `external_search.candidates_fetched` → 「候補{n}件を取得」/
     `external_search.evidence_selected` → 「根拠{n}件を選別」/
     `internal_search.completed` → 「関連記事{n}件を確認」。
   - 「最新」= recentEvents の末尾（古い→新しい順の最後）。
   - イベント変化で `router.refresh()` しない（Slice 4 の規律のまま。
     local state 更新のみ）。polling 間隔も 2s 据置。

## API Contract（/api-contract + /gen-types）

```text
GET /api/v1/research/runs/{runId}
  200: { runId, threadId, status, errorCode, progressStage,
         recentEvents: ResearchRunEvent[] }        # 追加（古い→新しい、最大 10 件）

ResearchRunEvent（type で discriminate）:
  { type: "internal_search.started",           ts, queryCount: number }
  { type: "internal_search.completed",         ts, hitCount: number }
  { type: "external_search.queries_generated", ts, taskIndex: number, queries: string[] }
  { type: "external_search.candidates_fetched",ts, taskIndex: number, candidateCount: number }
  { type: "external_search.evidence_selected", ts, taskIndex: number, evidenceCount: number }
```

他 endpoint・thread 詳細契約は不変。frontend Route Handler は中継のみで
変更なし（/gen-types 再生成のみ）。

## 実行フロー

```text
worker run_agent_answer:
  acquire（running 遷移）
  → publisher = AgentRunLiveEventPublisher(get_redis(), run_id)
    → publisher.reset()（前試行イベント DEL、best-effort）
  → build_question_answering_agent(..., progress=writer, events=publisher)
  → answer() 内（retrieving 中、internal/external 並列）:
      各検索サービスが節目で event_occurred(event)
      → publisher: LPUSH+LTRIM+EXPIRE（失敗は warning のみ）
  → 完了/失敗 tx は従来どおり（Redis は触らない。TTL で自然消滅）

API GET /runs/{runId}:
  read_run_for_user（所有権確認、404 収束）
  → AgentRunLiveEventReader: LRANGE 0 9 → 逆順 → entry ごとに検証
    （壊れた entry は skip / Redis 例外は [] + warning）
  → ResearchRunResponse(recentEvents=...) を返す

frontend ActiveRunStatus:
  2s polling → status/progressStage/recentEvents を local state 更新
  → running × retrieving の間だけ最新 1 件をサブテキスト表示
  → terminal 検知 → router.refresh()（従来どおり）
```

## New Types / Structure

```text
backend/app/agent/contract.py               (AnswerProgressEvent union + AnswerEventReporter)
backend/app/agent/internal_retrieval/service.py (events field + started/completed 発火)
backend/app/agent/external_search/runner.py     (events 注入 + per-task 3 イベント発火)
backend/app/agent/composition.py                (events 引数の配線)
backend/app/agent/history/live_events.py        (新規: publisher/reader + key・cap 50・TTL 900・read 10 の定数)
backend/app/queue/tasks/agent_run.py            (publisher 生成 + acquire 後 reset + 注入)
backend/app/agent/router.py                     (GET /runs に reader 結線)
backend/app/schemas/research.py                 (ResearchRunEvent union + recent_events)
frontend/src/features/research/components/
  ActiveRunStatus.tsx                            (最新 1 件サブテキスト + event→文言写像)
frontend/src/types/*.gen.ts                      (/gen-types 再生成)
docker-compose.test.yml                          (redis-test 追加 — 検証の制約参照)
```

## Invariants

- Redis は non-authoritative: 読めない・空でも API は 200 を返し、劣化は
  サブテキストが出ないことのみ。run の状態機械・DB 行には一切影響しない。
- publish / reset / read の失敗 log・metric に event payload（クエリ・件数含む）を
  載せない。`run_id` + `event type` のみ（Slice 4 progress writer と同じ規律）。
- イベントに元質問の本文・回答断片を入れない。例外は、元質問と異なることを確認した
  `question.resolved.standalone_question` と、`external_search.queries_generated` の
  `_clean_generated_queries` を通過した生成済み検索クエリだけ（internal のクエリ内容は
  出さない）。
- イベント payload の件数・文字数制限をイベント側に複製しない（external の
  cap はドメイン側が正本。cap 変更に自動追随する）。
- publish / reset / read は短い timeout で諦め、Redis の遅延・半断が worker の
  検索処理・`GET /runs` の応答時間をブロックしない。
- agent core（contract / 検索サービス）は Redis・run_id・時計を知らない
  （Protocol 越しの自工程申告のみ。ts は publisher が押す）。
- recentEvents の read は run 所有権確認の後でのみ行う。
- 会話データの描画は thread 詳細一本のまま（polling から描画するのは
  status + progressStage + recentEvents の ephemeral 表示のみ）。
- reporter / publisher は DB connection を使わない（pool と無関係。Slice 4 の
  tx 衛生不変条件はそのまま）。

## Non-goals

- SSE / Redis Stream / incremental resume（capped list で開始、の合意どおり）。
- 失敗系イベント語彙（provider 失敗・selector 失敗等。既存の task_reports /
  missingAspects / errorCode の責務）。
- イベント語彙の網羅（最小 5 種で開始。前方互換規約で後から追加）。
- `ResearchMessageRun`（thread 詳細）への recentEvents 同乗。
- pipeline_events / DB への焼き込み（consumer 不在）。
- polling 間隔の変更（2s 据置）。
- ミニログ UI・イベント履歴表示（最新 1 件のみ）。

## Tests

backend:

1. 発火（stub reporter）: internal — search_articles で started(query_count) →
   completed(hit_count) の順・**イベントにクエリ文字列が含まれない** /
   hits 0 でも completed(0) / reporter None で no-op。external —
   _search_task 成功経路で queries_generated → candidates_fetched →
   evidence_selected の順・queries が clean 済みの値 /
   query 生成失敗で以降イベントなし / provider 全滅で candidates_fetched
   以降なし / reporter None で no-op。
2. publisher（実 Redis）: LPUSH 順（最新が先頭）/ 51 件目で最古が落ちる
   （`LTRIM 0 49` で常に 50 件以下に保たれる）/ TTL が付く / reset() で
   key が消える / Redis 断（fake）で例外を握り warning log のみ・payload 非漏洩
   （capture_logs でクエリ文字列が log に無いこと）/ 応答しない Redis（hang
   fake）で timeout により短時間で諦め warning のみ。
3. reader（実 Redis）: 古い→新しい順・最大 10 件 / 壊れた JSON・未知 type の
   entry を skip して残りを返す / key 不在で [] / Redis 断・timeout で [] +
   warning（payload 非漏洩）。
4. API 統合: recentEvents が polling 応答に載る（空・非空）/ 他 user の run は
   404 のまま（イベント read に到達しない）/ OpenAPI に discriminated union が
   出る（/gen-types）。
5. worker 統合: acquire 後に前試行のイベントが消えている（reset 経由）。

frontend:

6. ActiveRunStatus: running × retrieving で最新イベントの文言がサブテキスト
   表示される / synthesizing 移行で消える / recentEvents 空なら stage 文言のみ /
   未知 event type は無視して直近の既知イベントを表示 / イベント変化で
   refresh されない（既存テストは全部 green のまま）。

## 検証の制約

- **テスト基盤: docker-compose.test.yml に redis-test を追加する**（現在は
  db-test のみ）。backend テスト品質改善プランで「rate limit 系は ephemeral
  実 Redis 採用」と合意済みの方針の先行適用。fakeredis 等の新規 dependency は
  追加しない。
- dev は実 LLM 不可のため、イベント発火の実観測は unit（stub/fake）中心。
  dev では recentEvents 空配列経路（Redis 空 / 断）の確認。全語彙の実観測は
  本番 deploy 後。
- **production Redis ACL（2 user 構成）で vector-core / worker-agent が使う
  user に `agent:run:*` の read/write 権限があるか deploy 前に確認**。
  不足していれば ACL 更新（本 slice の deploy 手順に含める）。
- `/check` + `/gen-types`。`/migration` 不要。

## Done

- 実行中（情報収集中）の user message に「“…” など 3 件を検索中」等の
  サブテキストが polling で live 表示され、synthesizing 移行で消える。
- Redis を止めても run は完走し、API は recentEvents: [] で 200 を返す
  （UI は stage 文言のみに劣化）。
- 再配送復旧 run に前試行のイベントが表示されない。
- publish / read の失敗 log にクエリ等の payload が載らない。
- 親仕様 提案 2 の PII 行が改訂されている。
- 新規テスト green + 既存 suite green + /gen-types 済み。
