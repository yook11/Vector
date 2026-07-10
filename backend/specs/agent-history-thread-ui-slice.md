# Agent thread 一覧・詳細・削除・キャンセル + frontend 結線 slice 仕様 (Slice 3)

## 位置付け

親仕様: `specs/agent/conversation-history-async-runs.md`。前提 slice: Slice 1（schema）+
Slice 2（非同期 run 実行・保存経路）— いずれも実装・**commit 済み**
（e85885cc / 08439569）。
本 slice は **thread 一覧 / 詳細 / DELETE / run キャンセルの API 群 + frontend 初結線
（/research 画面・polling・送信/停止/削除 UI）** を作る。progress_stage は Slice 4、
Redis ライブイベントは Slice 5。

親仕様からの差分（会話で合意済み、本仕様が正）:

- **run キャンセルを追加**する（error_code 語彙に `cancelled` を追加）。
- **`GET /runs/{runId}` から `result` を削除**し polling 信号に痩せさせる（描画は
  thread 詳細一本に統一）。フロント未結線のため in-place 変更（親仕様合意の範囲内）。
- 一覧 pagination は page/perPage（既存 `PaginationParams`）+「さらに表示」で確定。

## Problem

会話は Slice 2 で保存されるようになったが、読む手段（一覧・詳細）、消す手段（DELETE）、
待つのをやめる手段（キャンセル）がなく、frontend も未結線。ユーザーが画面から
質問 → 進行表示 → 回答閲覧 → 履歴再訪 → 削除まで一通り行える状態にする。

## 前提整理（Slice 2 レビュー残の回収、本 slice 冒頭で実施）

1. **read projection の拡張**: `app/agent/conversations/projection.py` と
   `app/agent/runs/projection.py` は**既存**
   （`build_research_run_response` = run + result の写像を所有）。残作業は
   thread 詳細写像（messages / sources / run 同乗）の追加と、slim 化に伴う
   result 写像の削除（Slice 2 設計判断 12 の回収先。新規作成ではない）。
2. **保存 mapper の集約**: assistant message 行の構築が repository インラインと
   mapper に分裂している → `AnswerQuestionResult` → rows（message + sources）を
   mapper の単一ファクトリに閉じる（Slice 2 設計判断 14 の完全化）。
3. **str → Literal の `# type: ignore` 除去**: DB の str を StrEnum で一度 parse して
   型を通す。
4. **`mark_failed` の遷移条件を呼び出し元別に**: router の enqueue 失敗時は
   `queued` からのみ（kiq が「raise したが実は配送済み」の race で実行中 run を
   誤 fail しない）。worker の生成失敗時は従来どおり active（queued/running）から。
5. **`get_agent_persistence_session` に理由コメント**: `get_session`（request 1 tx の UoW）
   と 2 tx 制御が両立しないための正当な逸脱であることを 1 文で残す。
6. **テストギャップ回収**: title 50/51 字境界 / worker 想定外例外 → `internal_error`。

engine `hide_parameters=True`（exfil 指摘）は全体設定のため**本 slice 外**（別小 PR）。

## Evidence（調査済みの既存規約・構造）

- **1 user message ⇔ 1 run（total）**: user message は `create_user_run` でのみ作られ
  必ず run を伴う。`uq_agent_runs_user_message` で 1:1 を構造強制済み
  （`app/models/agent_run.py:52`）→ 詳細契約で user message に run を同乗できる。
- **CASCADE 連鎖**: thread 削除で messages / runs / sources が全て消える
  （`agent_messages.thread_id` / `agent_runs.thread_id` ondelete CASCADE、
  sources は message 経由）。実行中 worker は run/thread 行の不在・終状態を
  先に検知して skip（`complete_run` の早期 return、通常経路）し、読取と UPDATE の
  間の狭い race も rowcount 0 → `RunTransitionLostError` で tx 全体 rollback —
  **どちらの経路でも回答は残らず、active run 中の削除は構造的に無害**。
- **error_code に DB 語彙 CHECK は無い**（Text + `failed ⇔ error_code` 双方向 CHECK のみ）
  → `cancelled` の追加に migration 不要。y1 も無変更。
- **pagination 前例**: `PaginationParams`（page/perPage、`app/schemas/base.py:22`）+
  `PaginatedArticleResponse`（items/total/page/perPage/totalPages、
  `app/schemas/articles.py:97`）。
- **active run 判定**: partial unique index `uq_agent_runs_thread_active` が
  queued/running を索引済み → 一覧の hasActiveRun は EXISTS で安価。
- **frontend 規約**（`frontend/CLAUDE.md`）: Server Components デフォルト /
  状態は URL searchParams / mutation は全て Server Action / **useEffect での
  データフェッチ禁止（Server Components または Route Handlers）** / backend 呼び出しは
  hey-api interceptor client 経由（server-side 専用）/ features 境界
  （`features/<backend ドメイン名>`、deep import 禁止）。
- **proxy rate limit**: `/api/*` は proxy.ts の application-level rate limit を通る
  （read class）。2 秒 polling がこの read tier に収まることを実装時に確認する。

## 設計判断

1. **キャンセル = `failed` + `error_code='cancelled'`**（5 番目の status は増やさない）。
   sweeper の `stale` が先例 — この機能の error_code は「回答なしで終わった理由の語彙」
   であり、cancelled は同族。状態機械・partial unique index・CHECK・冪等ガード・
   sweeper が一切無変更で済む。「エラーではないのに error_code」という命名の緊張は
   認識した上で、列の実態 = terminal reason として受け入れる（表示文言は frontend が
   errorCode で分岐する — Slice 2 設計判断 6 の継承）。
2. **キャンセル endpoint**: `POST /api/v1/research/runs/{runId}/cancel`
   （所有権 join 必須）。queued/running → failed('cancelled') の条件付き UPDATE。
   - rowcount 1 → 204
   - rowcount 0 で completed → 409（回答が既に在る。frontend は refetch して回答を表示）
   - rowcount 0 で failed（cancelled 含む）→ 204（冪等。「待つのをやめる」意図は
     満たされている。元の error_code は上書きしない — 終状態不変）
   - 不所有・不存在 → 404
3. **生成の協調中断はしない（non-goal）**。running 中のキャンセルは生成を走り切らせ、
   worker の完了 tx が**結果を構造的に破棄**する — 通常経路は終状態の先読み検知で
   skip（早期 return）、狭い race でも条件付き UPDATE rowcount 0 →
   `RunTransitionLostError` で rollback（Slice 2 の全か無か機構がそのまま
   キャンセルの安全装置になる。テスト期待値は経路でなく「回答が残らない」に置く）。
   節約できるのは生成コストのみで、taskiq に協調キャンセルの仕組みも無いため見送る。
4. **キャンセルしても user message は残す**（「キャンセルしました」マーカー + 再送導線）。
   メッセージごと消す案は履歴の append-only を崩し削除経路が 2 本になるわりに
   得るものが無い。
5. **thread 詳細は message 単位の discriminated union（role で判別）**:
   - user message には `run {runId, status, errorCode}` を同乗（1:1 total が
     構造保証済みのため常に非 null）。queued/running=スピナー、
     failed=errorCode 別の文言表示、runId は polling とキャンセルのハンドル。
     top-level の activeRun フィールドは持たない（messages から導出可能）。
   - assistant message には `sources` / `missingAspects` を同乗。
   - message 順は seq ASC。`seq` を含める（描画 key / 順序）。message pagination は
     しない（個人 thread の会話長で問題になってから）。
6. **`GET /runs/{runId}` は polling 信号に痩せさせる**: `result` を削除し
   `{runId, threadId, status, errorCode}` のみ。描画は thread 詳細 refetch 一本
   （2 描画経路を作らない）。これにより `ResearchResponse` schema は消費者を失うため
   **削除**し、read projection の写像先は詳細の `ResearchAssistantMessage` に一本化する。
   repository の `_read_completed_result` 相当は projection module へ移った上で
   run read から外れる。
   **C-3 の marker 契約は移設して保持する**: 削除する `ResearchResponse.answer`
   の Field description（`[[N]]` marker ↔ `sources[].sourceRef` の対応。SSoT =
   field description で生成型 JSDoc に届く、C-3 確定事項）を
   `ResearchAssistantMessage.content` に同等の description として付け直す。
   これを落とすと frontend が content を parse する契約文書が生成型から消える。
7. **一覧は専用 `ResearchThreadListParams` を新設**（page/perPage、default 20・max 100）。
   既存 `PaginationParams` は default 24 のため、再利用すると仕様の 20 と
   API/test/frontend の表示件数がずれる。受け方は既存規約どおり
   `Annotated[..., Query()]`（`Depends()` 禁止 — feedback_vo_boundary）。
   envelope は `PaginatedArticleResponse` と同形（items/total/page/perPage/totalPages）。
   並び順 `updated_at DESC`（同値 tiebreak: id DESC）。行は
   `{threadId, title, updatedAt, hasActiveRun}` — hasActiveRun はサイドバーの
   実行中バッジ用（EXISTS、Evidence 参照）。
8. **DELETE は active run 中も許可**（物理削除、204）。409 で守ると「実行中は消せない」
   UX になり最悪 stale 回収の 20 分待ちを強いる。構造安全は Evidence 参照。
   404 収束（不所有・不存在）は既存パターン踏襲。
9. **frontend 構成**: `features/research/`（backend ドメイン名に一致）。
   ルートは `(protected)` 配下に `/research`（一覧 + 空状態）と
   `/research/[threadId]`（詳細）。左サイドバー（一覧）+ 右チャットビューの 2 ペイン。
   サイドバーは server component として両ページで compose する
   （layout は searchParams を読めないため layout には置かない）。
   削除は確認ダイアログを挟む（物理削除のため）。実装は frontend-ui-builder agent に分担。
10. **「さらに表示」= searchParams の limit 成長方式**: `?limit=20`（default）→
    クリックで +20（`perPage=limit, page=1` で再取得）。client 側の append 蓄積を
    持たないため updated_at 並び替えによる重複/欠落が原理的に起きず、
    「Server Components + URL searchParams」規約にも一致する。上限は既存
    `MAX_PER_PAGE=100` — 到達時はボタンを消す（100 件超の閲覧手段が無いことは
    既知の bound として Non-goals に明記。必要になったら cursor 化を判断）。
11. **polling は Next Route Handler 経由**: client での backend 直接 fetch と
    useEffect フェッチは frontend 規約で禁止のため、
    `app/api/research/runs/[runId]/route.ts`（GET、interceptor client で
    backend の slim run response を中継）を新設し、client の polling hook は
    これを 2 秒間隔で叩く。**polling は信号専用** — terminal（completed/failed）を
    検知したら `router.refresh()` で server components（詳細 + サイドバー一覧）を
    再描画して停止する。Slice 5 の recentEvents はこの polling 応答に同乗する
    （親仕様どおり）。
    - **cache 制御は全段 no-store を明示**: client fetch（`cache: "no-store"`）/
      Route Handler の response header（`Cache-Control: no-store`）/ backend 中継の
      sdk 呼び出し（`cache: "no-store"`、get-pipeline-status 前例）。状態監視用のため
      どこか 1 段でも cache されると古い running / 古い terminal を見続ける。
    - **useEffect 禁止規約との関係を明文化**: polling hook 自体は timer + fetch に
      useEffect を使う。これは「会話描画データではなく run 信号のみを Route Handler
      から取得する**限定例外**」であり、会話・一覧データの描画フェッチは引き続き
      Server Components 一本（frontend/CLAUDE.md NEVER 8 の趣旨 = 描画データの
      client fetch 禁止、と整合させる読み）。
12. **polling の停止規律**:
    - polling 対象は server 描画から導出（詳細の messages に active run があれば
      その runId を client component へ props 渡し。refresh 後 active が消えれば
      props が null になり自然停止する自己安定ループ）。
    - タブ非表示（`document.hidden`）で完全停止、visible 復帰で即 1 回 poll +
      継続。DB SSoT のため取りこぼしは構造的に無い。
    - ネットワーク/5xx エラーは指数バックオフ（2s → 4s → 8s、上限 10s）、
      成功で 2s に復帰。
    - 404（別タブで thread 削除等）は停止して `router.refresh()`。
    - **401/403（セッション失効等）も停止して `router.refresh()`**
      （(protected) の認証リダイレクトに任せる。バックオフで叩き続けない）。
13. **mutation は全て Server Action**: 送信（POST /responses）/ キャンセル / 削除。
    送信は新規 thread なら `redirect(/research/{threadId})`、既存 thread なら
    refresh（polling が新 run を拾う）。active run 中は入力欄を無効化し
    「停止」ボタンを表示（server 側 409 が backstop、UI は先回りするだけ）。
    キャンセルの 409（直前に完走）は refresh に倒す（回答が現れる）。
    **204 でも表示を「キャンセルしました」に直結させず、refresh 後の最終 errorCode で
    決める** — worker が先に internal_error 等へ倒した race では 204 でも表示は失敗になる。

## API Contract（/api-contract + /gen-types）

```text
GET /api/v1/research/threads?page&perPage      (認証: get_current_user)
  200: {
    items: [{ threadId: uuid, title: str, updatedAt: datetime, hasActiveRun: bool }],
    total: int, page: int, perPage: int, totalPages: int
  }
  # 並び順 updated_at DESC, id DESC。perPage default 20 / max 100（既存上限）。
  # 自分の thread のみ。0 件は items: [] の 200。

GET /api/v1/research/threads/{threadId}        (認証: get_current_user)
  200: {
    threadId: uuid, title: str,
    messages: [
      { role: "user", seq: int, content: str, createdAt: datetime,
        run: { runId: uuid,
               status: "queued"|"running"|"completed"|"failed",
               errorCode: str | null } },
      { role: "assistant", seq: int, content: str, createdAt: datetime,
        sources: [...],            # 既存 ResearchSource union（internal articleId nullable）
        missingAspects: [str] },
    ]  # seq ASC、role discriminated union
  }
  404: 不所有・不存在

DELETE /api/v1/research/threads/{threadId}     (認証: get_current_user)
  204: 物理削除（CASCADE。active run 中も許可 — 設計判断 8）
  404: 不所有・不存在

POST /api/v1/research/runs/{runId}/cancel      (認証: get_current_user)
  204: queued/running → failed('cancelled')、または既に failed（冪等）
  409: 既に completed (detail: "Run already completed")
  404: 不所有・不存在
  # 204 は「run が回答なしで終わっている」ことしか保証しない。worker が直前に
  # internal_error 等へ倒した race では error_code は cancelled にならない。
  # 表示は必ず refresh 後の最終 errorCode で決める（設計判断 13）。

GET /api/v1/research/runs/{runId}              (変更: result 削除 — 設計判断 6)
  200: { runId: uuid, threadId: uuid, status: ..., errorCode: str | null }
  404: 不所有・不存在

# error_code 語彙: generation_unavailable | internal_error | enqueue_failed
#                  | stale | cancelled（追加。StrEnum + API Literal 両方）
```

## 画面とデータフロー

```text
/research                : サイドバー（一覧 20 件 + さらに表示）+ 空状態（質問入力）
/research/[threadId]     : サイドバー + チャットビュー（messages + 入力欄）

送信:   input → Server Action(POST /responses) → 202 {threadId, runId}
        → 新規: redirect(/research/{threadId}) / 既存: refresh
        → 詳細に user message + run(queued) が現れ polling 開始
polling: client hook → GET /api/research/runs/{runId}（Route Handler 中継、2s）
        → terminal 検知 → router.refresh()（詳細 + 一覧を再描画）→ 停止
停止:   「停止」ボタン → Server Action(POST cancel) → refresh
        → refresh 後の最終 errorCode に応じて表示（cancelled=「キャンセルしました」/
          他の errorCode=失敗文言。204 を「キャンセルしました」に直結させない —
          worker が先に failed へ倒した race では cancelled にならない）
削除:   確認ダイアログ → Server Action(DELETE) → redirect(/research) + refresh
```

## New Types / Structure

```text
backend/app/agent/conversations/projection.py (既存拡張: 詳細 message 写像を追加)
backend/app/agent/runs/result_mapper.py        (assistant message 行の構築を集約 — 前提整理 2)
backend/app/agent/conversations/repository.py (list_threads / read_thread_detail /
                                               delete_thread を user_id 拘束で追加)
backend/app/agent/runs/repository.py          (cancel_run追加、read_run_for_user slim化)
backend/app/agent/runs/types.py               (AgentRunErrorCode.CANCELLED 追加)
backend/app/agent/router.py                 (threads GET×2 / DELETE / cancel POST 追加)
backend/app/schemas/research.py             (thread list/detail schema +
                                             ResearchThreadListParams(default 20) 追加、
                                             ResearchRunResponse slim 化、ResearchResponse 削除。
                                             answer の marker Field description は
                                             ResearchAssistantMessage.content へ移設 — 設計判断 6)

frontend/src/features/research/             (新規 feature: api/ = Server Action + fetch、
                                             components/ = サイドバー/チャット/polling hook/
                                             確認ダイアログ)
frontend/src/app/(protected)/research/page.tsx
frontend/src/app/(protected)/research/[threadId]/page.tsx
frontend/src/app/api/research/runs/[runId]/route.ts   (polling 中継 Route Handler)
frontend/src/types/*.gen.ts                 (/gen-types 再生成)
```

DB schema 変更なし（`/migration` 不要。error_code は語彙 CHECK を持たない — Evidence 参照）。

## Invariants

- 認証必須。thread/run の read/write/delete/cancel は必ず user_id で絞る（不一致 404）。
- GET はプロセス内状態を持たず rows のみから導出（親仕様 Invariant 4）。
- run の終状態は不変。キャンセルを含む全遷移は条件付き UPDATE
  （cancelled が completed を上書きしない・逆も然り）。
- 削除は thread 配下で完結（CASCADE）。他 user・他 thread の行に波及しない。
- 描画経路は thread 詳細一本。polling 応答から会話を描画しない。
- frontend: 生成領域（*.gen.ts）手動編集禁止 / mutation は Server Action /
  client から backend 直接 fetch しない（Route Handler + interceptor client 経由）/
  features 境界維持。
- 例外の内部文言を error_code / API detail に leak させない（Logfire のみ）。
- テスト目的で認証・制約・冪等ガードを無効化しない。

## Non-goals

- 生成の協調中断（設計判断 3。キャンセルは「待つのをやめる + 結果破棄」まで）。
- cursor pagination / 一覧 100 件超の表示（設計判断 10 の既知 bound）。
- thread rename・検索・フィルタ・message 単位削除・専用 retry endpoint
  （再送 = 同じ質問をもう一度送る）。
- progress_stage 表示（Slice 4）・Redis recentEvents（Slice 5）。
- 過去履歴を agent 入力に使う（Phase 2）。
- engine `hide_parameters=True`（別小 PR）。
- Fly 本番デプロイ・VM 実測（deploy はユーザー操作）。

## Tests

backend（test-writer agent に分担）:

1. 一覧: 自分の thread のみ / updated_at DESC / perPage default 20・max 100 /
   totalPages / hasActiveRun（queued・running で true、terminal のみで false）/ 0 件 200。
2. 詳細: seq ASC / user message に run 同乗（4 status × errorCode null 規約）/
   assistant message に sources・missingAspects / 記事削除後 articleId null /
   404（他人・不存在）。
3. DELETE: 物理削除で messages/runs/sources が CASCADE 消滅 / 404 /
   2 回目は 404（物理削除後の不存在収束。cancel の「failed → 204」冪等とは
   異なり DELETE は冪等応答にしない）/ **active run 中の削除後、worker の完了経路が無害終了し
   assistant message / sources が一切残らない**（期待値は不変条件に置き、
   行不在 skip か RunTransitionLostError rollback かの実装分岐に固定しない。
   Slice 2 テスト 10 の変種）。
4. cancel: queued → cancelled / running → cancelled / completed → 409 /
   failed(既存) → 204 で error_code 不変 / 404（他人の run）/
   **cancel 後に worker が完走しても completed で上書きされない**（終状態不変）。
5. GET runs slim: result フィールドが消えている / 4 status の形。
6. 前提整理の回収: title 50/51 境界 / worker 想定外例外 → internal_error /
   mark_failed が router 経路で queued からのみ遷移（running を誤 fail しない）。
7. projection 移設後も Slice 2 の既存テストが green（写像の等価性はスイートで担保）。
8. OpenAPI: 新 endpoint 群 + slim 化 + cancelled 語彙が生成型に届く（/gen-types）。

frontend（既存 test 規約に従う。vitest、server-only は `.node.test.ts`）:

9. Server Action（送信/キャンセル/削除）の呼び出し形と 409/404 分岐。
10. polling hook: terminal で停止 + refresh / hidden で停止・visible で再開 /
    バックオフ / 404・401 で停止。
11. 一覧・詳細コンポーネントの状態表示（スピナー / エラー文言 / キャンセル済み /
    さらに表示ボタンの出し分け）。

## 検証の制約

- dev は egress 制約で実 LLM E2E 不可（既存 slice と同じ）。dev compose で
  worker 起動 + 構成エラー経路（queued → failed）を使い、UI から
  送信 → polling → failed 表示 → キャンセル/削除の実疎通を確認する。
  completed 経路の UI 確認は fake/seed データまたは本番 deploy 後。
- proxy rate limit の read tier が 2s polling を許容することを確認
  （不足なら間隔を調整して親仕様の 2s を更新する）。
- `/check` + `/gen-types`。`/migration` は不要。

## Done

- 画面から 質問送信 → 実行中表示 → 回答閲覧 / 停止 / 失敗表示 → 履歴一覧再訪 →
  thread 削除 が一通り機能する（dev で failed 経路まで実疎通）。
- polling は active run があるときだけ走り、terminal・タブ非表示・404 で止まる。
- Slice 2 レビュー残（前提整理 1〜6）が回収されている。
- 既存 suite green + 新規テスト green + /gen-types 済み。
