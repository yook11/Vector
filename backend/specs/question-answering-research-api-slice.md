# Research API endpoint 実装 slice 仕様 (Slice C-2)

## 位置付け

Q&A エージェントのコア工程は実装済みだが router 未配線で、ユーザーから
到達できない。本 slice で HTTP リクエスト → `QuestionAnsweringService.answer()`
→ レスポンスの同期貫通を作る。real planner と internal retrieval は
ここで初めて本結線になる。

前提 slice: C-1 (並列 retrieval)。後続 slice: C-3 (インライン引用。
**API の形は C-3 でも変わらない** — answer 文字列の中身に `[[N]]` marker が
増えるだけ)。

## Problem

- `QuestionAnsweringService` はどこからも wire されていない (router /
  composition 未接続)。
- probe は stub planner 固定で、`QuestionPlanningService` →
  `QuestionAnsweringService` の実結線が存在しない。
- internal retrieval は host probe から DB port の都合で貫通できて
  いない。API 経由 (アプリ内) なら DB に届く。

## Evidence (調査済みの既存規約)

- **Router**: 新 BC は `app/<context>/router.py` 方式
  (`app/insights/trend_discovery/router.py` 前例)。`APIRouter(prefix=...,
  tags=[...])` 自己完結、`app/main.py:188-194` で include。
  `main.py:222-224` が全 route の operation_id を関数名に上書き
  (関数名は app 内ユニーク必須、frontend 生成型の名前になる)。
- **Schema**: SSoT は `app/schemas/` の Pydantic v2。API 応答は
  `_CamelBase` (`app/schemas/base.py:12-19`) 継承で camelCase 出力。
  内部 contract (`app/agent/contract.py`) は素の BaseModel であり
  **直接 response_model にしない** (内部型の変更が API 契約に直結しない
  防壁)。
- **認証**: `get_current_user` (`app/dependencies.py:106`) →
  `CurrentUser(id, role)`。per-user endpoint の前例は
  `app/routers/watchlist.py:33`。
- **エラー形**: `{"detail": "..."}` 一貫。ドメイン例外 → HTTP はグローバル
  exception handler 方式 (`app/exceptions.py` + `app/exception_handlers.py`
  + `main.py:184-186`)。detail は allowlist / generic 化で leak 防止。
  **502/503 を返す前例なし** (新設になる)。
- **DI**: DB は `get_session` (`dependencies.py:42`)。Service factory は
  router 内ローカル関数の前例 (`articles.py:29`)。AI SDK (約133MB) は
  「非 AI プロセスに import させない」契約が
  `tests/test_lazy_ai_sdk_import.py` で pin されている →
  **module top で adapter を import すると API プロセス起動時に SDK が
  import される**ため遅延 import 必須。
- **SDK leak (実機確認済み)**: `import app.agent` だけで google SDK が
  ロードされる。原因は 1 箇所: `app/agent/__init__.py` →
  `planning/service.py:10` が adapter module (`planning/ai/gemini.py`)
  から error 型 `QuestionPlannerResponseInvalidError` を import している。
  answering 側は SDK-free base error (`AnswerDraftGenerationInvalidError`
  in `synthesis.py`) を adapter が継承する構造でこれを回避済み (鏡写しの
  正本)。`app/analysis/ai_provider_errors.py` は SDK-free (実機確認済み、
  endpoint から安全に import できる)。
- **タイムアウト**: frontend 内部 fetch timeout 15s
  (`frontend/src/lib/api/hey-api.config.ts:26`)、Fly proxy 明示設定なし
  (idle 60s 系)。数十秒同期 endpoint の前例なし (重処理は全て queue 202)。
- **rate limit**: backend API 経路に per-user rate limit なし。BFF proxy
  層 (session×IP two-tier、POST=mutation) が既に縛る。
  `AIModelRateLimitPolicy` gate は worker 専用配線 (API から再利用可能な
  作りではある)。

## 合意済みの設計判断

1. **同期 v1**。job 投入 (202+polling) / SSE は採らない。まず貫通させて
   実測レイテンシを取り、frontend 15s / Fly 60s と衝突するなら非同期化を
   別 slice で検討する (URL 設計はそれに耐える形にしてある)。
2. **`POST /api/v1/research/responses`** / operation_id
   `create_research_response` / router は `app/agent/router.py`
   (`prefix="/api/v1/research"`, `tags=["research"]`)。
   - URL は実装アーキテクチャ (`agent`) でなくドメイン (`research`) を
     表す。既存の名詞リソース規約 (`/articles`, `/trends`) に整合。
   - `responses` は「このリクエストに対する生成結果 object」。将来
     async 化しても `research/responses/{id}` へ**破壊的変更なしで拡張
     できる** (id 追加は互換変更)。
   - direct 経路 (検索なし回答) もこの endpoint から返る (リサーチ応答の
     縮退ケースとして許容)。
3. **request は `question` のみ**。strip + min_length=1 +
   **max_length=1000** → 超過・blank は **422 で拒否**
   (切り詰めない — 黙って切ると質問の意味が変わったまま回答してしまう)。
   422 は FastAPI 標準 validation 形のまま。**超過メッセージの文言は
   frontend の責務** (maxLength は OpenAPI 経由で生成型に届き、frontend が
   入力欄で事前強制する)。`as_of` はサーバー側で
   **`datetime.now(UTC)` (timezone-aware)** — クライアントから受けない。
   既存 agent の実装・テストは aware 前提で、prompt は `isoformat()` を
   そのまま使う。
4. **response はユーザー価値のあるフィールドだけ**。観測値を返さない:
   - 返す: `answer` / `sources` / `missingAspects`。
   - 返さない: `status`/`sufficiency` (観測値。logfire/audit に既にある) /
     `retrieval.plannedMode`・`unmetRequirements` (plannedMode も観測値。
     unmet はユーザー向け文言として missing_aspects に織り込み済みで
     二重になる)。
   - `missingAspects` は観測値ではなく回答の一部 (「この観点は根拠が
     取れなかった」という正直な申告) なので返す。
   - direct 回答は `sources: []` + `missingAspects: []` の素の回答。
5. **sources は回答との紐づけを表す**: 実際に引用された根拠だけが入る
   (引用照合で構造的に保証済み。飾りの検索結果一覧ではない)。
   `sourceRef` が結合キーで、C-3 のインライン marker `[[N]]` がこれを
   参照する。`snippet` が「その根拠が何を言っているか」を示す。
6. **エラーマッピング**: `AIProviderError` / `DirectAnswerInvalidError` →
   **endpoint 内 try/except で `HTTPException(503)`** + generic 固定
   detail **`"Answer generation is temporarily unavailable"`** (既存
   detail の英語スタイルに整合。内部理由は logfire のみ、leak させない)。
   **グローバル exception handler にはしない**: `AIProviderError` は
   `app.analysis` の汎用基底で、app-wide handler に answer 専用文言を
   紐づけると将来他 BC から漏れた同型例外にも適用される。producer が
   1 endpoint しかない例外を global に登録しない (consumer-driven)。
   専用例外で包む案も採らない (try/except の局所化で breadth 問題が
   構造的に消え、包む層が不要)。前例のない status を 2 つ増やさず 1 つに
   束ねる。想定外は既存どおり 500。ここで `answer()` 全体の例外 surface
   が API 境界で確定する (B-2 で先送りした文書化の回収先)。
7. **DI は request-scoped Depends factory** (router 内、articles の
   service factory と同型)。**factory 内で AI SDK を遅延 import**
   (queue composition の鏡写し)。internal search は `get_session`、
   Tavily 用 safe httpx client は yield 型 Depends でライフサイクル管理。
   DeepSeek / Tavily key の欠落は **構成ミスとして fail-fast** し、
   `AIProviderConfigurationError` → 503 generic detail に変換する。
   外部検索を無効化して `missingAspects` に見せる縮退はしない
   (failure visibility を優先)。
   **external agent 数は未指定 (None) を渡す** — resolver
   (`external_search/service.py:79-81`) が None を「task 数ぶんフル並列
   (hard limit 3 で丸め)」に解決するため、API 側の定数は不要。agent 数は
   並列度のみでコストは task 数 (planner が決める) に比例し、同期 API の
   レイテンシ最小化にはフル並列が正しい。
8. **audit recorder は v1 未配線** (None)。metrics は synthesis / direct
   service 内で既に発火する。DB 監査 (pipeline_events) は consumer が
   現れてから (consumer-driven)。
9. **real planner の本結線**をここで行う。`plan_question` compatibility
   helper (申し送り) は削除し、呼び出し側は `QuestionPlanningService`
   を直接使う。
10. schema 確定後 **/gen-types** で frontend 型を同期する (frontend UI
    実装は本 slice に含めない)。
11. **rate limit は BFF 層 + 認証 + max_length のみ (確定)**。backend
    AI gate の API 経路導入は見送り。BFF proxy が session×IP two-tier で
    POST を既に縛っており、認証必須 + 文字数 cap で一次防衛は成立する。
    gate 導入は 1 リクエスト内で複数モデルの policy を acquire する
    配線が必要で v1 の複雑さに見合わない (コスト実測後に別判断)。
12. **タイムアウトは v1 で endpoint 側に何も足さない (確定)**。下流
    (httpx safe client / AI SDK) の timeout に任せ、`asyncio.timeout`
    等の endpoint 独自機構は追加しない。probe / 本番で実測し、Fly proxy
    60s との衝突が常態なら非同期化 slice で対応する。frontend 15s は
    frontend slice の責務 (この endpoint だけ per-request 延長)。
13. **schema 型名 (確定)**: モジュールは `app/schemas/research.py`。
    request `ResearchQuestionRequest` (question のみ) / response
    `ResearchResponse` / sources は `ResearchInternalArticleSource` |
    `ResearchExternalUrlSource` (kind 判別、union alias `ResearchSource`)。
    クラス名がそのまま frontend 生成型名になるため `Research` prefix で
    名前空間を揃える。写像は
    `ResearchResponse.from_result(result: AnswerQuestionResult)`
    classmethod (`PaginatedArticleResponse.create()` の前例に整合)。
14. **`app.agent` package の import surface を SDK-free にする (前提
    作業)**。leak は `planning/service.py` が adapter module から error 型
    を import している 1 箇所 (Evidence 参照)。answering の鏡写しで、
    SDK-free な error 型を planning の工程 module 側に移し、adapter が
    それを継承/使用する形に直す。これにより router は `app/agent/router.py`
    のまま置け、`app/schemas/research.py` からの `app.agent.contract`
    import (package `__init__` が走る) も安全になる。
    `tests/test_lazy_ai_sdk_import.py` の流儀で **`import app.main` (API
    プロセスの実 import surface) を pin** する (加えて `import app.agent`
    も可)。

## API Contract

```text
POST /api/v1/research/responses     (認証: get_current_user)

Request  (app/schemas/research.py, _CamelBase):
  question: str    # strip, min_length=1, max_length=1000

Response 200 (_CamelBase):
  answer: str                     # C-3 以降は [[N]] marker を含みうる
  sources: list[Source]           # 実際に引用された根拠のみ, kind 判別 union
    - kind="internal_article": sourceRef, articleId (公開 /news id),
      title, sourceName, publishedAt, snippet
    - kind="external_url": sourceRef, url, title, sourceName,
      publishedAt, snippet
    # sourceName / publishedAt / snippet は nullable required:
    # キーを省略せず常に返し、存在しない metadata は null
    # (ArticleBrief.summary_preview の既存方針に整合)
  missingAspects: list[str]       # 不足観点の正直な申告 (空あり)

Errors:
  401  未認証 (既存標準)
  422  validation (blank / 文字数超過, FastAPI 標準形)
  503  AIProviderError | DirectAnswerInvalidError
       (detail: "Answer generation is temporarily unavailable")
  500  想定外 (既存標準)
```

## New Types / Structure

```text
backend/app/schemas/research.py       (新規: request/response schema, _CamelBase)
backend/app/agent/router.py           (新規: router + Depends factory 群 + 503 変換)
backend/app/main.py                   (include_router のみ)
backend/app/agent/planning/service.py (SDK-free 化: adapter error import の除去 / plan_question 削除)
backend/app/agent/planning/ai/gemini.py (error 型の移設先変更に追従)
backend/tests/test_lazy_ai_sdk_import.py (import app.main の pin 追加)
```

- 内部 `AnswerQuestionResult` → API schema への写像は
  `ResearchResponse.from_result()` classmethod (設計判断 13)。

## Invariants

- 認証必須。認可・認証ロジックの簡略化・迂回をしない。
- 内部 contract 型を response_model に直接使わない。
- 観測値 (sufficiency / plannedMode / unmet) を API response に含めない。
- sources に引用されていない根拠を混ぜない (既存の引用照合を通った
  結果だけを写像する)。
- 503 の detail から内部理由 (プロバイダ名・例外文言) を leak させない。
- AI SDK を API プロセスの module import 時に import しない。
  `import app.main` が SDK-free であることをテストで pin する
  (`tests/test_lazy_ai_sdk_import.py` の契約の API への拡張)。
- `as_of` は timezone-aware (UTC)。naive datetime をパイプラインに
  流さない。
- 秘密情報は settings 経由。question 以外のユーザー入力を受けない。
- DB 変更なし (migration なし)。

## Non-goals

- frontend UI 実装 (別 slice。/gen-types による型同期までが本 slice)。
- 非同期化 (job / polling / SSE)。実測後に判断。
- backend 側 per-user rate limit / AI gate の API 経路導入 (設計判断 11)。
- endpoint 独自のタイムアウト機構 (設計判断 12)。
- インライン引用 (C-3)。
- レスポンスの永続化・履歴 (responses に id を付けるのは async 化時)。
- progress event / timeline。

## 検証の制約

- dev の docker backend は egress なし → **dev での実 LLM E2E は不可**。
- 検証は 3 層:
  1. router unit テスト: `dependency_overrides` で fake agent を注入し、
     契約 (200 写像 / 401 / 422 / 503 / 500) を検証。
  2. host probe: 実 LLM で direct / external 経路の貫通 (internal は
     host から DB 不達のため対象外)。
  3. 本番デプロイ後の実確認 (internal 込み全経路 + レイテンシ実測)。

## Tests

1. 200: fake agent の `AnswerQuestionResult` が契約どおり camelCase で
   写像される (sources の kind 判別 / articleId / url を含む)。
2. nullable required: metadata 欠損 source で sourceName / publishedAt /
   snippet のキーが省略されず null で返る。
3. sources 空 + missing 空 (direct 相当) がそのまま返る。
4. missingAspects が返る (insufficient 相当。status は response に
   現れないことも assert)。
5. as_of: fake agent が受け取る `as_of` が timezone-aware (UTC) である。
6. 401: 未認証。
7. 422: blank / 文字数超過 (境界値 1000/1001)。
8. 503: fake agent が `AIProviderError` を raise → generic detail、
   内部文言が detail に含まれない。
9. 503: `DirectAnswerInvalidError` も同様。
10. 503: DeepSeek / Tavily key 欠落は構成ミスとして generic detail
    (外部検索不能への縮退はしない)。
11. 500: 想定外例外は既存挙動 (503 に化けない)。
12. lazy import: `import app.main` で AI SDK が import されない
    (`tests/test_lazy_ai_sdk_import.py` の流儀で pin)。
13. OpenAPI: operation_id `create_research_response` / maxLength が
    schema に出る。

## 実測後の follow-up (本 slice の Done には含めない)

- 本番実測レイテンシが frontend 15s / Fly proxy 60s と衝突するかの確認。
  衝突が常態なら非同期化 slice (`research/responses/{id}` への互換拡張)。
- コスト実測を見た backend AI gate 導入の再判断 (設計判断 11)。

## Done

- `POST /api/v1/research/responses` が認証付きで疎通し、fake agent の
  router unit テストが green。
- real planner + internal retrieval + external search + synthesis /
  direct の全結線が composition として存在する。
- endpoint 局所の 503 変換で typed error が generic detail で返る。
- DeepSeek / Tavily key 欠落は 503 となり、外部検索不能への縮退で隠れない。
- `plan_question` compatibility helper が削除されている。
- /gen-types 済みで frontend 型に契約が届いている。
- host probe で direct / external 経路の実貫通を確認 (実行はユーザー環境)。
- 既存 suite に regression なし。
