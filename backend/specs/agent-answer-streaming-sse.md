# Agent 回答ストリーミング / SSE 親仕様

## 位置付け

本仕様は、非同期 research run の待機体験を改善するための親仕様である。
既存の会話履歴・progress stage・Redis ライブイベントの実装を土台に、回答の
生成途中テキストを安全に表示する仕組みを定義する。後続の実装は本仕様を小さな
slice に分割して行い、ここで直接コードは変更しない。

関連する既存仕様:

- `agent-history-run-execution-slice.md`: 非同期 run、完了時の一括保存
- `agent-history-thread-ui-slice.md`: thread UI、202 応答、polling
- `agent-run-progress-stage-slice.md`: DB に保存する粗い工程状態
- `agent-run-live-events-slice.md`: polling に同乗する検索中の細粒度イベント
- `question-answering-evidence-synthesis-slice.md`: evidence 回答の検証・retry・fallback
- `question-answering-inline-citation-slice.md`: 本文内 citation marker と出典整合性

既存の `recentEvents`（Redis List + polling）は、実装を切り替えるまで維持する。
この仕様は回答断片を `recentEvents` に加えない。SSE 導入用の ephemeral な配信
経路を別に持つ。

## Problem

現在の research run は、質問を受理して worker へ投入した後、`planning` /
`retrieving` / `synthesizing` と検索イベントを polling で表示する。最終回答は
生成、引用検証、DB 保存がすべて完了してから初めて thread 詳細に現れる。

そのためユーザーは「何をしているか」は一部把握できても、最も待ち時間の印象に
影響する「回答を書き始めた」ことを確認できない。回答生成中の文章を段階的に表示
しつつ、最終回答の根拠・引用・永続化の正しさを損なわない必要がある。

## Goals

1. run 開始直後から、現在の工程と検索中の作業を表示する。
2. 回答生成が始まったら、ユーザーに見せる本文の下書きを低遅延で追記表示する。
3. 最終化時には、DB に永続化された検証済みの回答・citation・sources へ置き換える。
4. 一時的な接続断、Redis の障害、ブラウザの再訪・再接続で run の正しさを失わない。
5. 既存の BFF 認証、run 所有権、非同期 worker、DB を正本とする境界を保つ。

## Evidence

- `QuestionAnsweringService` は `planning`、`retrieving`、`synthesizing` の境界を
  報告し、worker は別トランザクションで `agent_runs.progress_stage` を更新する。
- `ActiveRunStatus` は `GET /runs/{runId}` を 2 秒間隔で polling し、進捗と
  `recentEvents` を一時表示する。会話本文の正本は thread 詳細である。
- worker は Taskiq 上で `agent.answer()` を実行し、成功後にだけ assistant message /
  source rows / run の完了状態を同一トランザクションで保存する。
- `acquire_for_execution()` は queued / running のrunを再取得し、そのたびに
  `agent_runs.started_at` を更新し、`attempt_epoch` をDB内で原子的に1増やす。worker
  timeoutは300秒であり、重複配送時には旧workerと新workerのeventがRedis上で交差し得る。
- direct 回答は plain text だが、evidence 回答は `sufficiency`、`answer`、
  `cited_refs`、`missing_aspects` を持つ structured JSON である。後者は marker と
  evidence の整合検証に失敗すると retry または fallback になる。
- frontend から backend への認可済み通信は Next.js BFF が短命 JWT を付与して行う。
  ブラウザが private backend へ直接接続する設計にはしない。
- 現行の Redis ライブイベントは、回答断片を保存しないことを明記している。
  回答下書きは同じ key / List / polling 契約へ追加せず、別の寿命・再開要件を持つ。

## User experience

run 中の user message の直下に、次の二層を表示する。

1. **工程表示**: 「計画中」「情報収集中」「回答作成中」と、検索中だけは最新の
   細粒度イベントを補助文として表示する。これは現在の UI の責務を引き継ぐ。
2. **回答下書き**: `synthesizing` 以降に assistant 側の下書き領域を表示し、受信した
   テキストを追記する。表示ラベルは「回答を作成中」とし、確定回答と区別する。

完成前の下書きには sources 一覧、citation のリンク・プレビュー、missing aspects を
表示しない。Direct pathでは最終回答から除去される `[[1]]` のようなmarkerを増分経路でも
表示しない。Evidence pathの未確定citationはproviderのraw JSONを直接表示せず、後続sliceの
増分復元契約に従う。run 完了を受信したら thread 詳細を再取得し、既存の
`CitedAnswerContent` による検証済み表示へ置き換える。

失敗・キャンセル・再試行時は下書きを確定回答として残さない。失敗表示は既存の
error code に従い、キャンセル後の下書きは画面から除去する。物理的に provider の
処理を中断するかは、この仕様の初期範囲には含めない。

client はevent typeにかかわらず、現在値より大きい `attemptEpoch` を受信した時点で
表示中の下書きを破棄し、epochを更新してからeventを適用する。`attempt.started` は通常の
境界通知だが、markerのtrim・publish喪失・decode不良があっても正しさを失わないよう、
下書き破棄をmarkerの存在だけに依存させない。同一epochのmarker重複は破棄シグナルではない。

## Architecture

```text
browser EventSource
  -> Next.js BFF SSE route (session verification + short-lived BFF JWT)
  -> FastAPI SSE endpoint (run ownership verification)
  -> Redis Stream (run-scoped, bounded, TTL)
  <- worker live publisher (progress / activity / answer deltas / terminal)

worker
  -> answer validation + result persistence transaction
  -> Postgres (the only durable final answer)
  -> terminal event after the transaction commits
```

SSE を採る理由は、配信方向が server から browser の一方向だけだからである。
WebSocket の双方向接続・セッション管理は不要である。SSE の event ID と
`Last-Event-ID` を使い、接続が切れても未受信イベントから再開できるようにする。

## Live event transport

### Storage

run ごとに Redis Stream を持つ。key 名、TTL、最大長は実装 slice で既存 Redis ACL と
output token 上限を照合して確定するが、次を守る。

- key は run ID を含み、他 run と混ざらない。
- Stream は bounded かつ短期 TTL とし、会話履歴の保存先にしない。
- 1文字または1 provider chunkごとにRedisへ書かない。Direct pathは250msの時間窓を主条件、
  512 Unicode code pointを1 deltaの上限としてcoalesceし、最小文字数gateを設けない。
  Evidence pathの値は専用sliceで同じMAXLEN / timeout予算と合わせて決める。
- Stream の read / write / reset が失敗しても、回答生成・DB 保存・run 状態遷移を
  失敗させない。Redis は live 表示だけの補助データである。
- eventの帰属はRedis Stream IDや`attempt.started`の位置ではなく、全entryに入る正の整数
  `attemptEpoch` で決める。要求epochより小さいentryは旧workerとして捨て、等しいentryだけを
  返し、大きいentryは新attempt境界として通知する。epochはrun取得ごとにDBで1増える。

### Event vocabulary

イベントは表示文言でなく、型付き payload とする。日本語表示は frontend が所有する。
すべてのentryは共通envelopeとして `attemptEpoch` を持つ。Python内部のfield名は
`attempt_epoch`、SSEでのserialized field名は `attemptEpoch` とする。
以下の `{ ... }` はSSEへ出す公開data表記であり、Redis entryの `payload` fieldには
event固有の属性だけを入れる。`activity` は配信制御fieldとdomain payloadを分離するためnestedにする。
Redis Stream内のactivity固有fieldはPython domain modelのsnake_caseを保ち、JavaScriptへ公開する
SSE serializer境界でだけcamelCaseへ変換する。ケース変換のためにdomain modelをcamelCase化したり、
継承modelを作ったりしない。

```text
attempt.started
  { attemptEpoch }

stage
  { attemptEpoch, stage: "planning" | "retrieving" | "synthesizing" }

activity
  { attemptEpoch, activity: { type: 既存の検索イベント型, ...既存の安全な属性 } }

answer.delta
  { attemptEpoch, generation: positive integer, text: non-empty string }

answer.reset
  { attemptEpoch, generation: positive integer }

terminal
  { attemptEpoch, status: "completed" | "failed", errorCode?: existing run error code }
```

- `attempt.started` は worker が run の実行を取得した直後に送る。Redis timeoutで
  成否が不明な場合は、同じepochの次のpublish前にlazy retryしてよい。
- Stream ID を SSE の `id` にそのまま対応させる。client は受信済み ID を再適用しない。
- activity固有payloadは `activity` fieldへnestedし、frontendは `payload.activity.type` で
  discriminateする。SSE protocolの `event: activity` と混同するため、JSON field名に
  `event` は使わず、activity固有fieldもtop-levelへflattenしない。
- `answer.reset` は、生成結果の検証失敗による retry で以前の下書きを取り消すための
  イベントである。次の generation の delta だけを画面に表示する。
- `terminal` は DB の commit 後に publish する。publish 失敗時にも polling により
  terminal status を検知して thread 詳細を再取得できる。
- cancel endpoint が run を terminal にした時も、browser が直ちに下書きを閉じられる
  よう terminal を配信する。worker から遅れて届いた delta は terminal 後に UI 状態を
  変更してはならない。
- `terminal.status` は既存run状態と同じ `completed` / `failed` だけである。cancelは
  `failed` と `errorCode="cancelled"` の組で表す。

### Data exposure

`answer.delta` は、run 所有者にだけ短時間配信するユーザー向け本文である。これは
既存 `recentEvents` の「回答断片を含めない」規則とは別の、明示された例外である。

次の情報は Stream と SSE に流さない。

- 元質問本文、会話文脈、prompt、system instruction
- chain of thought、内部の判定過程、未選別 evidence の本文
- provider request / response の生 payload、API key、例外メッセージ
- 最終化前の source metadata、citation のリンク情報

payload を log・metric・例外に記録しない。ログに残してよいのは run ID、event type、
generation、失敗分類など、本文を復元できない診断情報だけである。

## Server-sent events contract

FastAPI は次の private API を提供する。

```text
GET /api/v1/research/runs/{runId}/events
Accept: text/event-stream
Last-Event-ID: <optional Redis Stream ID>
```

処理順序は必ず以下とする。

1. BFF JWT を検証する。
2. run と user の所有権を DB で確認する。存在しない、または他者の run は 404 に収束する。
3. DBから得た1以上の `attempt_epoch` をreaderへ渡し、`Last-Event-ID` があればその直後、
   無ければ現epochの保持済みeventから読む。小さいepochは送らず、大きいepochを観測したら
   境界前cursorを維持して同じ接続を観測epochへre-pinする。attempt取得transactionはRedis
   publishより先にcommitされるため、re-pinのためのDB再取得は行わない。
4. Stream を SSE frame (`id`, `event`, JSON `data`) に変換して送信する。
5. stream冒頭で `retry: 1000` を送り、10秒間隔のheartbeat commentを送る。
6. backend request受付から45秒で接続を閉じ、terminalを送信した場合も接続を閉じる。

response は `text/event-stream`、`Cache-Control: no-store, no-transform` とし、中継・CDN による
buffering を避けるヘッダを deployment slice で検証する。Redis が読めないときは
200開始前なら503、開始後ならeventを合成せず接続を閉じ、frontend は既存pollingの工程表示へ
劣化する。SSE固有のcontrol eventは追加しない。SSE障害が
run の失敗やDBの状態変更を引き起こしてはならない。

browser は backend に直接接続せず、同一 origin の Next.js Route Handler に
`EventSource` 接続する。BFF は session を検証し、接続ごとに発行した短命 JWT と
`Last-Event-ID` を private backend へ中継する。SSE 専用の upstream fetch は既存の
15 秒 API timeout を使わない。長期接続の中断・cleanup を明示的に扱う。

SSEの接続開始は通常read rate limitと別bucketにし、BFFでsession+run 12回/分、session
30回/分、IP 120回/分を同時に適用する。open connectionの最終上限はbackendの各API processで
run 2、user 4、process 50とする。run / user超過は429、process超過は503で開始前に拒否し、
全終了経路でslotをresponse EOF前に解放する。このsliceではdistributed counterを追加せず、
1 Uvicorn process/Machine・API Machine最大2を運用制約とするため、全体100は厳密なglobal上限
ではなく運用上のceilingである。BFF limiterのRedis障害はfail-openするが、request class別の
metricとPIIを含まない固定event logへ記録する。

45秒のmax age closeは下書き破棄条件ではない。同じEventSourceは `retry: 1000` と
Last-Event-IDで約1秒後の再接続を開始し、同じepochならdraft、current epoch、適用済みevent IDを
維持して差分だけを続ける。大きいepochのeventを受信した場合だけepoch境界としてdraftを破棄する。

## Generation and finalization

### Direct path

direct 回答はplain textのため、Geminiのasync streaming APIから届く増分textをworker内で
全文へ集約しながら`answer.delta`としてpublishする。表示経路ではchunkをまたぐcitation markerと
最終結果でstripされるouter whitespaceを増分除去し、正常時のdelta連結を最終
`DirectAnswerDraft.answer`と一致させる。

`generation`は`DirectAnswerFlow`のattempt number 1〜2を使う。in-request retryはmarker / whitespace
除去後のblankだけであり、そのgenerationは可視deltaを0件とするため、Direct pathから
`answer.reset`を送らない。250ms / 512文字coalescing、2秒cache付きcancel / stale worker抑止、
3連続publish失敗時のdelta専用circuit breakerは
`agent-direct-answer-deltas-slice.md`を正本とする。

全テキストは従来どおり検証して最終結果へ渡す。Redis障害やdelta breakerは最終validation、
DB保存、terminal producerを停止しない。

### Evidence path

evidence 回答は structured JSON を使い、`answer` だけでなく sufficiency、引用、
missing aspects を最終検証する。structured output の途中 chunk は完全な JSON ではない
ため、provider の chunk をそのまま UI に出してはならない。

この経路の実装 slice では、次の二択を明示的に決める。

1. **下書き許容方式（初期推奨）**: JSON stream から `answer` field の有効な文字列だけを
   増分復元して下書き表示する。最終の構文・citation 検証で retry になれば
   `answer.reset` を送り、次の generation を表示する。
2. **安定本文方式**: text-only の回答ストリームと、最終 metadata を確定する責務を
   分離する。この方式は再生成を見せない代わりに synthesis contract の再設計または
   追加の model call が必要になる。

本親仕様では下書き許容方式を採用する。つまり下書きの書き換えは正しい UX として
許容し、最終回答だけを権威あるものとする。JSON の増分復元器は独立した小さな部品にし、
JSON escape、Unicode、field 順序、途中切断、retry のテストを必須とする。

### Finalization order

1. worker は最終テキストを集約し、既存の validation / retry / fallback を実行する。
2. `AnswerQuestionResult` を既存の一括 transaction で assistant message / sources /
   run completed として保存する。
3. commit 成功後に `terminal(completed)` を publish する。
4. frontend は thread 詳細を再取得し、下書き領域を永続化済み assistant message に
   差し替える。

run が failed / cancelled になった場合、terminal は DB の terminal 遷移後に publish
する。provider の実行が遅れて完了しても `complete_run()` の既存の条件付き遷移が
assistant message を保存しないことを維持する。

## Boundaries and invariants

- Postgres の `agent_messages` / `agent_message_sources` / `agent_runs` が会話と最終結果の
  唯一の正本である。Redis Stream の内容から thread 詳細を復元しない。
- progress stage は低頻度・復元価値ありの DB 状態、live event は高頻度・短命の表示用
  データ、最終回答は DB の永続データという分類を保つ。
- agent core は Redis、run ID、SSE、時計、HTTP を知らない。回答生成時の通知は
  `AnswerProgressReporter` / `AnswerEventReporter` / `AnswerDeltaReporter` と分離した
  optional protocol越しに行い、workerがrun固有のpublisherとcontinuationをbindする。
- streaming reporter の失敗は回答生成を止めない。publisher は短い timeout で諦め、
  DB connection や長いトランザクションを保持しない。
- attemptの帰属は整数epochの大小比較で決める。新attempt開始後に旧workerが遅延publishしても、
  小さいepochはreaderが返さず、大きいepochは切替境界になる。Stream IDとmarker位置は
  再開・表示通知だけの責務である。
- 6種類すべてのeventのepoch増加をclientの下書き破棄境界とし、markerだけへ依存しない。
- SSE 接続は開始時に必ず所有権を確認する。event ID を推測して他者の payload を読む
  経路を作らない。
- 再接続・再配送・retry で同じ delta が複数届いても、client は ID と generation により
  重複表示しない。
- max ageによる定常再接続では同じepochのdraftを消さず、loading表示へ戻さず、deltaを
  二重適用しない。
- transport readerはterminal後に同epochのentryが届いても捨てない。terminalを受信した
  consumerが、その後の表示更新を無視する。
- Direct pathのcitation markerは下書きにも最終回答にも描画しない。citation link、source card、
  missing aspectsは最終DB結果からだけ描画する。
- 既存の polling は SSE 接続不可時、terminal publish 失敗時、初期 rollout 時の
  degradation path として残す。

## Non-goals

- LLM の思考過程、tool call、prompt、内部検索全文を表示すること。
- 回答下書きのDB永続化、下書きからの会話復元、ページをまたぐ下書き編集。
- 双方向 WebSocket、リアルタイム共同編集、stream の consumer group。
- 初期リリースでproviderの実行を物理的にcancelすること。Direct pathはcancel / stale検知後に
  ローカルiterator消費をbest-effortで止めるが、provider server側の停止・課金停止は保証しない。
- citation 品質や回答品質そのものを改善すること。
- 既存 `recentEvents` に回答断片を追加すること。
- Redis Stream を監査ログや長期分析データとして使うこと。
- `agent_runs.attempt_epoch` 以外のDB schema migration。永続cursorや下書き保存が必要に
  なった時点で別途Ask Firstとする。

## Suggested slice boundaries

実装は次の順で分ける。各 slice は Problem / Evidence / Invariants / Done を個別に
定義し、前の slice の検証が完了してから次へ進む。

現在は1〜5まで実装済みであり、次は6のEvidence answer draft deltasへ進む。

1. **Live stream transport**: Redis Stream の key / TTL / ACL / publisher / reader、
   event vocabulary、実Redisでの replay・timeout・payload非露出テスト。
2. **Attempt epoch fencing token**: DB採番の正整数epoch、ゾンビworker除外、
   `attempt_advanced` と境界非消費。
3. **SSE backend and BFF**: FastAPI の所有権確認付き SSE、Next.js の同一 origin proxy、
   reconnect / heartbeat / terminal close。新しい外部API契約のため `/api-contract` を使う。
4. **Live event producer wiring**: 既存の DB stage / Redis List activity を残したまま
   Redis Stream へ二重書きし、DB commit 後の completed / failed / cancelled terminal を
   best-effort で接続する。専用仕様は `agent-live-event-producer-wiring-slice.md`。
5. **Direct answer deltas**: plain text generatorのstreaming化、増分citation / whitespace除去、
   coalescing、cancel後の抑止、delta専用circuit breaker。専用仕様は
   `agent-direct-answer-deltas-slice.md`。
6. **Evidence answer draft deltas**: structured JSON の増分復元、retry 時の reset、
   citation検証後の確定表示。この slice は既存 synthesis contract の変更範囲を先に
   確認する。
7. **Research UI**: 工程表示と下書き領域、EventSource lifecycle、replay / reset /
   terminal / polling fallback、アクセシビリティ。
8. **Operational verification**: Fly の buffering・idle timeout、Redis ACL、observability、
   負荷・障害時の劣化、E2E。

## Verification

各 slice のテストに加え、親仕様として次を満たすことを確認する。

1. direct run で工程表示後に本文が追記され、完了後はDBの回答に置き換わる。
2. evidence run で途中の JSON 構文や未確定 citation がUIに露出しない。
3. evidence retry で古い下書きが `answer.reset` 後に残らない。
4. 接続断後に同じ run へ再接続しても、保持期間内の同一epoch eventは重複なく再開する。
   trim済みcursorは下書きの完全復元を主張せず、劣化状態として扱う。
5. 他ユーザーの run の SSE は 404 であり、Redis を読む前に拒否される。
6. Redis を止めても回答はDBに保存され、UIは既存 polling の工程表示へ劣化する。
7. cancel / failure 後に下書きが確定回答や sources として表示されない。
8. log、trace、metric、例外に回答本文や Stream payload が含まれない。
9. 新attemptのmarker後に旧epochのeventが届いても表示されず、同一epochのmarker重複では
   下書きが破棄されない。
10. BFFのSSE接続開始制限とbackendのrun / user / process同時接続上限が独立して働き、
    limiter劣化とcapacity拒否をpayload・user ID・run IDなしで観測できる。
11. activityはnested `activity` fieldとcamelCase属性で配信され、旧 `event` field、flatten、
    未知activity typeをfrontendへraw転送しない。
12. direct回答の正常配信では、同じgenerationのdelta連結が最終Direct回答と一致する。
13. marker / whitespaceだけのblank retryはdelta / resetを作らず、generation 2だけを表示する。
14. delta publishが連続失敗しても回答をDBへ保存し、terminalを別経路で試行する。
15. cancelまたはepoch前進を検出したworkerは、runをfailedへ再遷移させずroutineに停止する。

## Done

- 進行中の research run が、工程・検索状況・回答下書きを一貫して表示する。
- 下書きは短命かつ所有者限定で配信され、最終回答は常に既存DBの検証済み結果である。
- 切断、retry、cancel、Redis 障害、worker 再配送で回答の正本・引用整合性・所有権が
  壊れない。
- SSE が使えない場合も、既存の polling 表示と最終結果表示へ安全に劣化する。
- 接続開始頻度とopen connection数が別々に制限され、process単位の保証と運用上のglobal
  ceilingを混同しない。
- 各 slice のテスト、型生成、deploy 環境の検証が完了している。
