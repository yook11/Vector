# Agent run SSE backend / BFF slice 仕様（Draft）

## 位置付け

親仕様: `agent-answer-streaming-sse.md`。

前提slice: `agent-live-stream-transport-slice.md`、
`agent-attempt-epoch-fencing-token-slice.md`。

本sliceは回答ストリーミング導入の第二段階として、実装済みのRedis Stream readerを
所有権確認付きFastAPI SSE endpointへ接続し、Next.js BFFから同一originで中継できる
ところまでを扱う。

本sliceの終了時点ではbrowserの `EventSource` と回答下書きUIを接続しない。
また、回答生成側の `answer.delta` / `answer.reset` / `terminal` producerも接続しない。
合成eventを使い、workerからbrowser直前までの配信経路と失敗時の境界を検証する。

本書はDraftである。認証・所有権・protocol integrity・接続の有限終了・圧縮防止に加え、
HTTP 200の意味、接続中のattempt切替、queued runへの接続、Redis劣化時のclose、
control eventを追加しない方針、native EventSourceの再接続規約、接続開始rate limit、
backendの同時接続capacity、`activity` のnested公開shapeを保証条件として採用済みである。

## Problem

Redis Streamへ型付きeventを保存し、cursorから再開する基盤は存在するが、現在は
HTTP経由で読む経路がない。browserはprivate backendへ直接接続できず、既存の
Next.js BFFもJSON API向けの15秒timeoutを前提としている。

このまま回答生成のstreaming化やUI実装を先に進めると、次の責務が同時に混ざる。

- run所有権と認証
- Redis Streamのcursor再開
- SSE frame・heartbeat・接続終了
- BFFの長時間stream中継と切断cleanup
- provider chunkの生成・coalescing
- Reactの下書きstateとpolling fallback

まず配信境界だけを独立させ、認証済みユーザーが所有するrunの短命eventだけを、
既存の最終回答・polling契約を変えずに中継できる状態を作る。

### Failure domain model

回答生成とライブ表示を別の障害domainとして扱う。

| domain | 正本と結果 | live経路の扱い |
|---|---|---|
| 回答生成の失敗 | Postgresのrunを `failed` にし、最終状態を保存する | DB commit後の `terminal(failed)` で速く通知する |
| ライブ表示だけの失敗 | run状態、回答生成、最終DB結果を変更しない | SSEをcloseし、既存pollingと最終DB結果へ劣化する |

Redis StreamとSSEは再構築可能な補助経路であり、障害をrun failureへ昇格させない。
`terminal` eventは通知速度のため、Postgres run statusは正しさのために使う。

## Evidence

- `app.agent.live_updates.stream.AgentRunLiveStreamReader` は、run ID、attempt epoch、
  optional cursorを受け、Redis Streamをnon-blockingで読む。
- readerは `events`、`empty`、`stream_missing`、`attempt_absent`、`attempt_advanced`、
  `cursor_trimmed`、`unavailable` を区別する。
- Redis entryは全て正のinteger `attemptEpoch` を持つ。小さいepochはskipされ、等しいepochだけが
  eventとして返り、大きいepochは `attempt_advanced` として通知される。
- workerはrun取得直後に `attempt.started` をpublishするが、現時点でstream publisherを
  agent coreへ注入していない。実際に発火するstream eventは `attempt.started` だけである。
- `AgentRunRepository.read_run_for_user()` はrunとthread ownerをjoinし、他ユーザーと
  不在runを同じ `None` に収束できる。ただし公開用 `ResearchRunResponse` は
  `attempt_epoch` を返さないため、SSE内部用の所有run contextが別途必要である。
- FastAPIの既存research endpointは `get_current_user` によりBFF JWTを検証する。
- frontendの既存run Route HandlerはJSON responseを返し、generated SDK経由のfetchは
  15秒timeoutを持つ。SSE upstreamへこのfetch経路を再利用できない。
- BFFはBetter Auth sessionから60秒の内部JWTを生成でき、`INTERNAL_API_URL` は
  private backendの許可済みhostへ制限されている。
- frontendのapplication-level rate limitは全 `/api/*` requestの開始回数を制限するが、
  ニュース閲覧を主対象とした通常read用bucketであり、45秒ごとに再接続するSSEを同じ財布へ
  入れると通常閲覧を不必要に圧迫する。SSEは専用の接続開始bucketへ分離する必要がある。
- 既存frontend limiterはRedis不通時にfail-openし、60秒間隔のwarnを出す。ただし、
  Redis評価時の劣化をrequest class別に集計するmetricはまだない。
- `backend/fly.core.toml` のAPI processは1 MachineにつきUvicorn 1 processで起動する。
  現在の運用上限をAPI Machine 2台とするなら、process上限50は全体の運用上限100に対応する。
- `next start` は既定でresponse圧縮を有効にし、現行 `next.config.js` は
  `compress: false` を設定していない。SSE Route Handler自身が変換・bufferingを防ぐ必要がある。
- backendにはGZip middlewareがないが、`SecurityHeadersMiddleware` は
  `BaseHTTPMiddleware` を継承する。disconnect保証はgenerator単体だけでなくapp全体で
  検証する必要がある。
- FastAPIのyield dependencyで開いたDB sessionはstreaming responseの終了までcleanupが
  遅れる可能性がある。SSE loop中にDB session / transactionを保持してはならない。
- workerは `attempt_epoch` を増やすtransactionのcommit後にだけRedis publisherを作る。
  したがって大きいepochをRedisで観測した時点では、そのepochのDB commitは完了している。
- native EventSourceは200で開始したstreamの終了時には同じinstanceで再接続する。一方、
  204 / 409 / 429 / 503など非200 responseはconnectionをfailさせ、HTTP status自体をJavaScriptへ
  公開しない。後続UIはstatus本文ではなく `error` と `readyState`、既存pollingを使う。

## Goals

1. run所有者だけがFastAPI SSE endpointを開始できる。
2. Redis Stream IDをSSE `id` として送り、`Last-Event-ID` の直後から再開できる。
3. event typeとJSON dataを安定したSSE frameへ変換する。
4. eventがない間もheartbeatを送り、terminalを送った接続を終了できる。
5. Next.js BFFがsessionを検証し、private backendのSSE responseをbufferせず中継する。
6. browserまたはBFFの切断をbackend / Redis readへ伝播し、資源を解放する。
7. SSEやRedisの障害がrun状態、回答生成、DBの最終結果、既存pollingを変更しない。
8. 次のproducer / UI sliceが接続できる明示的なHTTP・event契約を作る。
9. LLM本文や入力headerがSSE frame、event type、event ID、upstream requestを偽造できない。
10. terminal eventを失っても、全接続が最大接続時間内に必ず終了する。
11. BFFがSSE bodyを圧縮・bufferせず、eventを受信可能になった時点で逐次転送する。
12. 接続数と終了理由をpayload非依存の低cardinality metricsで観測できる。
13. transportの6 eventとheartbeat commentだけを送り、SSE固有のcontrol eventを追加しない。
14. BFFでSSE接続開始頻度を通常readと別に制限し、backendでopen connection数を有限にする。

## Guarantee priorities

### P0: confidentiality and protocol integrity

- BFF session、内部JWT、backend所有権確認の3境界を全て通す。
- 他者所有runと不在runを404へ収束し、所有権確認前にRedisを読まない。
- `role=admin` でもrun所有権条件を迂回しない。
- 検証済みrun ID / cursorだけをupstream URL / headerへ埋め込む。
- LLM本文を1行JSONへserializeし、SSE `id` / `event` / frame境界を偽造させない。
- SSE層は既知eventだけをallowlist投影し、未知eventやraw payloadを中継しない。

### P1: liveness and resource safety

- 全接続をterminal、最大接続時間、劣化状態、client切断のいずれかで有限時間内に終了する。
- terminal runへの新規・再接続は204で終了し、EventSourceの自動再接続を止める。
- 200開始後のRedis障害はpayloadを合成せず接続を閉じ、同じEventSourceの再接続preflightで
  回復またはpolling-onlyへの移行を決める。
- disconnectをBFF、middleware、backend generator、Redis waitまで伝播する。
- SSE responseをcache・圧縮・bufferしない。
- BFFでsession+run 12回/分、session全体30回/分、IP 120回/分の全tierを同時に適用する。
- backendの各API processでrun 2、user 4、process全体50の同時接続上限を原子的に強制する。
- run / user上限は429、process上限は503としてresponse開始前に拒否する。

### P2: observability

- active接続数、接続時間、close理由を観測する。
- capacity拒否を固定scope `run` / `user` / `process` で観測する。
- frontend limiterのRedis fail-openをrequest class別counterと固定event logで観測する。
- metric labelへuser ID、run ID、cursor、本文、JWT、Cookieを含めない。
- logは既存方針どおりrun IDと固定の失敗分類を許容するが、本文、cursor、JWT、Cookie、
  未知event type値を含めない。

## Scope

### In scope

1. FastAPI private SSE endpointを追加する。
2. BFF JWT検証後、Redisへ触る前にDBでrun所有権を確認する。
3. runの現在status / attempt epochをSSE内部contextとして取得する。
4. `Last-Event-ID` の形式検証とRedis reader cursorへの変換を行う。
5. 保持済みeventのreplay、追従read、heartbeat、terminal closeを実装する。
6. Redis eventをSSE `id` / `event` / `data` frameへserializeする。
7. Next.jsに同一originのSSE Route Handlerを追加する。
8. BFFでBetter Auth sessionを検証し、接続ごとに内部JWTを発行する。
9. BFFからbackendへ `Accept` / `Last-Event-ID` / Authorizationを転送する。
10. 既存15秒timeoutを使わないSSE専用upstream fetchとabort伝播を実装する。
11. backend / BFFの契約testと、合成Redis eventによるintegration testを追加する。
12. OpenAPI変更を確認し、必要なgenerated frontend typesを同期する。
13. frame serializerに1行JSON escapeと既知event allowlistを実装する。
14. 接続最大時間とterminal runへの204 responseを実装する。
15. active接続数・接続時間・close理由の低cardinality metricsを追加する。
16. `next start` とapp全体を使い、圧縮・buffering・disconnectをintegration testする。
17. queued runを200で開始し、短命DB再確認で正のepochまたはterminal状態を待つ。
18. `attempt_advanced` でcursorを維持したまま同一接続を新epochへre-pinする。
19. BFFに通常readと独立したSSE接続開始rate-limit planを追加する。
20. backendにprocess内のrun / user / process同時接続counterと確実なreleaseを追加する。

### Out of scope

- Geminiのstreaming APIへの切替。
- provider chunkの集約、delta coalescing、generation管理。
- `stage` / `activity` をRedis Stream publisherへ接続すること。
- `answer.delta` / `answer.reset` / `terminal` のproducer接続。
- cancel endpointからterminal eventをpublishすること。
- browser `EventSource`、React hook、下書きstate、画面表示。
- SSE失敗時のpolling fallback UI。
- `CLOSED` 後に新しいEventSourceを作り、過去instanceのcursorからlive配信を再開する機能。
- citation、sources、missing aspectsの途中表示。
- Redis List / `recentEvents` の変更・削除。
- DB schema / migration、永続cursor、下書き保存。
- Fly / CDNの実環境検証とproduction負荷試験。これらはoperational sliceで扱う。
- WebSocket、consumer group、consumer ack、exactly-once配信。
- Redis counter / per-connection leaseによる複数process共通の厳密な同時接続上限。
- API Machine 3台以上、または1 Machine内のUvicorn複数worker構成への拡張。
- globalなNext.js圧縮無効化。まずroute単位の `no-transform` とproduction testで保証し、
  不十分な場合だけ `compress: false` を別途判断する。

## Proposed architecture

```text
browser（後続UI slice）
  -> GET /api/research/runs/{runId}/events
     Next.js Route Handler
       1. Better Auth session確認
       2. runId / Last-Event-ID検証
       3. 短命BFF JWT発行
       4. request.signal付きupstream fetch
  -> GET /api/v1/research/runs/{runId}/events
     FastAPI
       1. BFF JWT検証
       2. DBでrun所有権とlive context確認
       3. DB session解放
       4. Redis Stream replay / follow
       5. SSE frame / heartbeat
  <- Redis Stream（短命、bounded、非正本）
```

認証・所有権確認と最初のrun context取得はresponse開始前に終える。DB sessionと
transactionはSSE generatorへ渡さず、長時間接続中に保持しない。

## API contract（Draft）

### FastAPI private endpoint

```text
GET /api/v1/research/runs/{runId}/events
Accept: text/event-stream
Authorization: Bearer <short-lived BFF JWT>
Last-Event-ID: <optional Redis Stream ID>
```

200 response:

```text
status: 200
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-store, no-transform
X-Accel-Buffering: no
```

`Connection` はHTTP/2やproxyでhop-by-hopに扱われるため、公開契約の必須headerには
しない。production proxyでbufferingされないことはoperational sliceで確認する。

HTTP 200は「SSE配信チャネルを開始できた」ことだけを表し、回答生成の成功を表さない。
回答生成の正本は常にPostgresのrun状態である。

response status:

| status | 条件 |
|---|---|
| 400 | `runId` または `Last-Event-ID` が許可する形式でない |
| 401 | BFF JWTがない、期限切れ、署名・issuer・audienceが不正 |
| 404 | runが存在しない、または現在ユーザーの所有物でない |
| 204 | runが既に `completed` / `failed` であり、最終DB結果へ移るべき |
| 429 | BFFのSSE接続開始bucket、またはbackendのrun / user同時接続上限を超えた |
| 409 | `Last-Event-ID` がtrim済みで、そのcursorから完全に再開できない |
| 503 | backend process上限、初回availability window後のstream不在、またはRedis読取不能 |
| 200 | queuedを有限時間待つ、またはrunning attemptのeventをreplay / followできる |

backendのcapacity拒否とlive availability由来の429 / 503には `Retry-After: 5` を付ける。
BFF自身の接続開始rate limitによる429は既存limiterが算出するwindow残秒を `Retry-After` に使う。
BFFはbackendの204 / 409 / 429 / 503と `Retry-After` をbody変換せず中継する。

native EventSourceでは200以外のresponseはconnection failureとなり、自動再接続しない。
HTTP statusはJavaScriptへ公開されないため、204 / 409 / 429 / 503の区別はserver contract、
非browser client、診断のために保持し、browser UIの分岐材料にはしない。503後に再試行する場合も
native EventSourceへ任せず、applicationが新しいinstanceを作る必要がある。本sliceでは
`CLOSED` になったrunをその画面内でpolling-onlyへ固定し、manual SSE retryは実装しない。

running runの初回readではresponse開始前にbounded availability windowを設ける。
`STREAM_MISSING` はworker開始raceとしてそのwindow内だけ再試行し、期限後は503とする。
`UNAVAILABLE` は503、`CURSOR_TRIMMED` は409とする。`EVENTS` / `EMPTY` /
`ATTEMPT_ABSENT` は200を開始する。`ATTEMPT_ADVANCED` は後述の規則でre-pinしてから続行する。

このendpointは既存APIへの追加であり、既存request / response shapeを変更しない。

### Next.js same-origin endpoint

```text
GET /api/research/runs/{runId}/events
Accept: text/event-stream
Last-Event-ID: <optional Redis Stream ID>
Cookie: <Better Auth session>
```

BFFは次を行う。

1. `runId` と `Last-Event-ID` をbackendと同じ規則で検証する。検証済み値だけを
   upstream URL / headerへ埋め込み、UUIDは小文字canonical表現へ正規化する。
2. Better Auth session storeを読む前に、未検証cookie tokenのhashとIPを使うSSE専用の
   接続開始rate limitを評価する。session+run 12回/分、session全体30回/分、IP 120回/分の
   利用可能な全tierをANDで適用し、いずれかの超過はsession readとupstream接続前に429へ
   収束させる。cookie tokenは単独の防御境界とせず、解決できる場合はIP tierを必ず併用する。
3. Better Auth sessionがなければupstreamへ接続せず401を返す。
4. sessionから内部JWTを発行し、Authorization headerとしてbackendへ送る。
5. browserからの `Last-Event-ID` を値を変えずbackendへ転送する。
6. upstreamのSSE bodyをJSON parse・再serializeせず、byte streamとして返す。
7. request abort時にupstream fetchとbody readをcancelする。
8. SSE専用fetchには既存の15秒timeoutを適用せず、request開始から50秒のhard timeoutを使う。
9. payload、Authorization、Cookie、response bodyをlogへ出さない。
10. responseに `Cache-Control: no-store, no-transform` を設定し、SSE bodyを圧縮・bufferしない。
11. backendの204をbodyなしで中継し、terminal runへの自動再接続を停止する。
12. backendの409 / 429 / 503と `Retry-After` をno-storeで中継する。
13. 公開responseにも `X-Accel-Buffering: no` を設定する。
14. browser abortは何もlogせず終了し、50秒timeoutまたはupstream network failureは
    例外本文を露出せず503 + `Retry-After: 5` へ収束させる。

SSE requestは通常read bucketから除外し、SSE専用bucketだけで数える。同じsessionがrun IDを
変えてsession+run bucketを回避できないようsession全体tierを、session値を変えても無制限に
ならないようIP tierを必ず併用する。IPを安全に解決できない場合など、既存limiterの
fail-open規則は維持するが、その劣化を後述の固定metricとlogへ記録する。

`runId` 検証はupstream URLへのpath injection、`Last-Event-ID` 検証はCR / LFを使った
header injectionを防ぐ境界である。malformed値はupstream fetch前に400へ収束させる。
Fetch / Headers実装が独自に拒否することへ依存せず、application contractとして検証する。
`Last-Event-ID` は `<milliseconds>-<sequence>` のcanonical decimal表現とし、両partを
unsigned 64-bit以下、全体を41文字以下に制限する。先頭zero、符号、空part、追加separatorを
許可しない。

BFFはbackendの認可を代替しない。session確認は同一origin入口のlogin gate、FastAPIの
所有権確認はprivate backendにおける最終的なauthorization boundaryである。

## SSE event mapping（Draft）

Redis Stream IDをSSE `id` にそのまま使い、transport eventのdiscriminatorをSSE `event` に
使う。`data` はUTF-8 JSON objectを1行でserializeする。

```text
id: 1710000000000-0
event: attempt.started
data: {"attemptEpoch":2}

id: 1710000000001-0
event: stage
data: {"attemptEpoch":2,"stage":"retrieving"}

id: 1710000000002-0
event: activity
data: {"attemptEpoch":2,"activity":{"type":"external_search.candidates_fetched","taskIndex":0,"candidateCount":12}}

```

公開event:

| SSE event | data |
|---|---|
| `attempt.started` | `attemptEpoch` |
| `stage` | `attemptEpoch`, `stage` |
| `activity` | `attemptEpoch`, nested `activity: AnswerProgressEvent` |
| `answer.delta` | `attemptEpoch`, `generation`, `text` |
| `answer.reset` | `attemptEpoch`, `generation` |
| `terminal` | `attemptEpoch`, `status`, optional `errorCode` |

`activity` は次のnested shapeで固定する。

```text
{
  attemptEpoch: positive integer,
  activity: {
    type: existing safe AnswerProgressEvent discriminator,
    ...activity固有の安全な属性
  }
}
```

JSON data内のfield名は `activity` とし、曖昧な `event` は使わない。これによりSSE protocolの
`event: activity`、DOMの `MessageEvent`、JSON data内のdomain payloadを区別する。
`attemptEpoch` は配信制御fieldとしてtop-levelに置き、activity固有fieldをtop-levelへflatten
しない。公開JSONはnested内もcamelCaseとし、frontendは `payload.activity.type` で型を絞る。

transportの6 eventを1:1で投影し、各eventには対応するRedis Stream IDを必ず付ける。
`stream.reset`、`stream.fallback`、`run.snapshot`、`stage.failed` のようなSSE専用eventや、
Redis Stream IDを持たない合成eventは追加しない。attempt切替と劣化は既存event、HTTP status、
接続close、Postgres pollingで表現する。

Redis内部の `publishedAt` とraw envelopeはSSEへ公開しない。質問、prompt、会話文脈、
chain of thought、未選別evidence、provider生response、secret、例外本文も送らない。

### Reconnection field

200 responseでは最初のeventまたは待機より前に、次のprotocol fieldを1回送る。

```text
retry: 1000

```

`retry` はeventではなく、native EventSourceのreconnection timeを1秒へ設定するprotocol fieldで
ある。MessageEventを生成せず、event IDを持たず、cursorを進めないため、「全公開eventはRedis
Stream IDを持つ」という契約の例外ではない。user agentは障害時に追加backoffを入れ得るため、
再接続時間を厳密な1秒SLAとはせず、1秒を要求する契約とする。

`retry: 1000` はmax ageだけでなく予期しない200 streamの切断にも適用される。capacity設計では
障害時に1 clientあたり最大約1 request/秒で再接続し得る前提を使う。HTTP responseの
`Retry-After: 5` とは別の契約である。

### Frame integrity

`answer.delta.text` はLLM生成物であり、SSE protocol文字列として信頼しない。全dataを
JSON objectとして1回だけserializeし、そのJSONを単一の `data:` 行へ置く。

- CR、LF、CRLF、NUL、quote、backslashはJSON string内でescapeする。
- `data:` 行の値にliteral CR / LFを含めない。改行はframe区切りとしてserializerだけが付ける。
- LLM本文をSSE frameへ直接文字列連結しない。
- `id` は検証済みRedis Stream ID、`event` はserver側のallowlistからだけ生成する。
- 本文中の `id:` / `event:` / `data:` / `retry:` は単なるJSON文字列であり、fieldにならない。

SSE投影層は次の6種類だけを明示的に扱う。

```text
attempt.started
stage
activity
answer.delta
answer.reset
terminal
```

transport unionが将来拡張されても、SSE projectorを更新しない限り新eventを公開しない。
未知eventはraw payloadを送らず、本文や未知type値を含まない固定の失敗分類だけを記録して
破棄する。

### Heartbeat

heartbeatはSSE comment frameとする。

```text
: heartbeat

```

heartbeatにevent IDを付けず、再開cursorを進めない。comment frameはJavaScriptへeventとして
dispatchされないため、後続UIはheartbeat受信時刻による無通信判定を行わない。heartbeatは
proxy / connection維持専用とし、client側のliveness backstopには既存pollingを使う。

最後のeventまたはheartbeat送信から10秒経過した場合にheartbeatを送る。eventを送信した場合は
heartbeat時計を更新し、不要なcommentを重ねない。

全接続はbackend request受付から45秒でcloseする。event、heartbeat、queuedからrunningへの遷移、
attempt re-pinによってdeadlineを延長しない。BFFはrequest開始から50秒でupstreamをabortし、
BFF JWTの60秒TTLまで10秒の余裕を残す。clockと各時間値はtestで短縮注入できる形にする。

### Timing contract

| 項目 | 値 |
|---|---:|
| Redis follow interval | 500ms |
| Redis read total timeout | 500ms |
| heartbeat interval | 10秒 |
| backend connection max age | 45秒 |
| BFF upstream hard timeout | 50秒 |
| initial `STREAM_MISSING` grace | 2秒 |
| mid-stream `STREAM_MISSING` grace | 2秒 |
| re-pin後 `ATTEMPT_ABSENT` grace | 2秒 |
| queued DB recheck interval | 2秒 |
| queued wait limit | 10秒 |
| HTTP 503 `Retry-After` | 5秒 |
| SSE `retry` | 1000ms |
| backend capacity lease TTL | 55秒 |

## Backend design（Draft）

### Owned run live context

公開用 `ResearchRunResponse` を流用せず、repositoryからSSE内部用のimmutable contextを返す。
最低限必要な候補fieldは次である。

```text
run_id
status
attempt_epoch = agent_runs.attempt_epoch
error_code
```

DB modelそのものをstream generatorへ渡さない。repositoryはrun IDとuser IDで所有権を
同時に絞り込み、不在と他者所有を同じ `None` に収束する。

### Connection lifecycle

1. JWTとheaderを検証する。
2. backend process全体のcapacity slotを取得し、上限ならDBへ触れず503を返す。
3. DBでowned run live contextを読み、DB sessionを閉じる。
4. terminal runならprocess slotを解放し、Redisへ触らず204を返す。
5. 所有権確認済みのrun / user capacity slotを原子的に取得し、上限なら全slotを解放して429を返す。
6. queued runならRedis commandが成功することだけを確認し、key不在は正常として200を開始する。
7. running runならresponse開始前に初回Redis readとbounded availability windowを完了する。
8. 200 response開始後、保持済みeventをID順に送り、cursorからfollowする。
9. eventがない間はheartbeat commentを送る。
10. terminal eventを送った場合は、それより後のentryを送らず接続を閉じる。
11. mid-streamのRedis障害ではeventを合成せず接続を閉じる。
12. client disconnect / task cancellation時はRedis waitとqueued DB waitをcancelする。
13. terminalを受信できなくても最大接続時間で接続を閉じる。
14. response EOFより前に全capacity slotを `finally` で解放する。
15. 自動再接続時にBetter Auth sessionとDB statusを再確認し、terminal runなら204を返す。
16. StreamingResponse自体も冪等releaseを所有し、body iterator未開始の切断でもslotを解放する。
17. response lifecycleへ到達しないraceのbackstopとして、次のcapacity取得前に55秒を超えた
    leaseをlazy janitorで除去する。

process上限は所有権DB readより前に判定するため、上限到達中の認証済み・入力正常requestは
runの存在や所有権によらず一律503となる。この順序はDBを飽和から守りつつ、503の差でrunの
存在を推測できないようにするための契約である。

queuedは `status = queued AND attempt_epoch = 0` で判定し、epoch 0をreaderへ渡さない。
Redisのavailability probeには接続用ACLで許可された `EXISTS` を使い、falseはstream未作成として
正常扱いする。200開始後は再確認ごとに新しい短命DB sessionを開いて閉じ、sessionやtransactionを
待機中に保持しない。

queued待機は次へ収束する。

- `attempt_epoch >= 1` のrunningになったらそのepochへpinし、Redis replay / followへ移る。
- completed / failedになったら何も合成せず接続を閉じる。自動再接続のpreflightが204を返す。
- DB再確認失敗またはqueued待機上限到達では接続を閉じる。
- client切断では待機taskとDB readをcancelする。

queued DB再確認は2秒間隔、最大10秒とする。各確認を重ねず、前回の短命sessionが閉じてから
次のintervalを待つ。10秒経過またはconnectionの45秒deadline到達の早い方でcloseする。

runningの初回 `STREAM_MISSING` は500ms間隔で最大2秒待ち、streamが現れなければ
503 + `Retry-After: 5` とする。200開始後の `STREAM_MISSING` とre-pin後の
`ATTEMPT_ABSENT` も500ms間隔で最大2秒だけ待ち、復旧または有効eventがなければsilent closeする。
Redis例外または500msのread timeoutは2秒待たず、即座に503またはsilent closeへ収束する。

terminal publishはbest-effortであり、streamにterminalが無いままDBだけがterminalに
なることを許容する。runningへ移った後は接続中のDB定期pollingを追加せず、最大接続時間を
backstopにする。最大時間後の自動再接続preflightでDB statusを読み、completed / failedなら
204でEventSourceを停止する。これによりrunning中のDB readを増やさず、接続も無限化しない。

後続UIは `terminal` を受信したら確定表示へ切り替えるためrun詳細を取得し、
`EventSource.close()` を呼ぶ。serverもterminal送信後に接続を閉じる。terminalを取りこぼした
場合は200 streamの終了後に同じEventSourceが自動再接続し、入口の204で停止してからrun詳細を
取得する。terminal eventは速さ、204 + Postgres再取得は正しさを担う。

max ageによるcloseはdraft破棄条件ではない。同じEventSourceは `retry: 1000` と内部の
Last-Event-IDを使って再接続し、同じepochなら保持中のdraftへ未適用eventだけを続ける。
後続UIはmax-age由来の `CONNECTING` でdraft、current epoch、適用済みevent IDを変更せず、
loading表示へ戻さない。再接続中に大きいepochを受信した場合だけepoch規則に従ってdraftを破棄する。

接続lifecycleは次の低cardinality metricsで観測する。名称は既存metrics規約に合わせて
実装時に確定するが、意味は固定する。

```text
active SSE connections gauge
SSE connection duration histogram
SSE close total counter { reason }
SSE capacity rejection total counter { scope }
```

`reason` は `terminal` / `max_age` / `unavailable` / `client_disconnect` /
`cursor_trimmed` などの固定enumだけを許可する。response開始前のcapacity拒否はcloseではないため
close reasonへ混ぜず、`scope = run | user | process` の専用counterで数える。

### Redis follow

既存readerのnon-blocking `read_after()` を共有Redis poolから読む。`EMPTY`、grace待機、
追従待機では500ms間隔とするが、`EVENTS` が続くcatch-up中はsleepを挟まず次pageを読む。
これにより保持上限4096件のbacklogを500ms/pageで不必要に遅延させない。1回の
`read_after()` は既存transport契約どおり、内部の複数commandを含めて500ms以内に収束させる。
同じ接続でreadを重ねず、結果処理とinterval待機を終えてから次のreadを開始する。

本sliceでは `XREAD BLOCK`、SSE専用Redis pool、接続ごとの専有Redis connectionを追加しない。
blocking方式はevent到着遅延とidle command数を減らせる一方、SSE接続数とRedis connection数を
直接結び付ける。まず短命operationとcapacity上限で資源を制御し、command rateの実測が問題に
なった場合だけ別sliceでblocking方式を再検討する。

follow sleep、Redis read、queued DB read、heartbeat待機はclient disconnect / task cancellationで
中断できなければならない。

### Connection capacity（採用済み）

接続開始頻度とopen connection数は別の資源なので、次の3層で保護する。

| layer | 単位 | 上限 | 超過時 |
|---|---|---:|---|
| BFF SSE start rate | session + run | 12回/分 | 429 + limiter算出の `Retry-After` |
| BFF SSE start rate | session全体 | 30回/分 | 429 + limiter算出の `Retry-After` |
| BFF SSE start rate | IP | 120回/分 | 429 + limiter算出の `Retry-After` |
| backend active connection / API process | run | 2 | 429 + `Retry-After: 5` |
| backend active connection / API process | user | 4 | 429 + `Retry-After: 5` |
| backend active connection / API process | process全体 | 50 | 503 + `Retry-After: 5` |

BFFの3 tierは利用可能なものを全て同時に適用する。SSE routeを通常read bucketから除外し、
45秒max ageによる定常再接続がニュース閲覧のbudgetを消費しないようにする。BFF limiterは
Redis障害時にfail-openする補助防御であり、backendの同時接続上限を最終境界とする。

backend capacityはRedisを使わず、API process内のcounterをasync task間で原子的に更新する。
取得順序は次で固定する。

1. run ID / cursorとBFF JWTを検証する。
2. process全体slotを取得する。取得できなければDBへ触れず503を返す。
3. DBでrunの存在、所有権、status、attempt epochを確認し、stream開始前にDB sessionを閉じる。
4. terminal runならprocess slotを解放して204を返す。
5. 所有権確認済みのuser ID / run IDでrun slotとuser slotを原子的に取得する。取得できなければ
   既に得たslotを全て解放し、429を返す。
6. Redis availabilityを確認し、開始可能なら200 responseを開始する。
7. terminal、max age、client disconnect、BFF abort、Redis error、generator exception、
   cancellationの全経路で `finally` により全slotを解放する。

run / user keyed counterは所有権確認後だけ作るため、不在runや他者runによる任意key増殖と
存在推測を防ぐ。正常なmax age再接続では、response EOFより前にbackend slotを解放し、
`retry: 1000` による次の接続を古いslotが誤って429へしないようにする。

process crashではmemoryとcounterが同時に失われるため、永続decrement漏れは生じない。
このsliceではRedis counter、per-connection lease、TTL refreshを追加しない。その代わり、
上限は各API processに対してのみ厳密であることを契約とする。現在はFly Machineごとに
Uvicorn 1 process、API Machine最大2台を運用制約とし、process上限50から導かれる100接続は
application全体の厳密なglobal上限ではなく運用上のceilingとする。API Machineを3台以上へ
増やす、またはUvicornを複数worker化する前に、この前提を再評価し、必要ならdistributed
leaseを別sliceで導入する。

#### BFF fail-open observability

BFF rate-limit Redisの未設定・接続失敗・eval失敗はrequestを許可するが、静かに防御を消さない。
既存の60秒throttle付きwarnを構造化し、次を記録する。

```text
frontend_rate_limit_fail_open_total{request_class="sse"|"read"|"mutation"}
frontend_rate_limit_redis_fail_open { request_class, error_type }
```

`request_class` と `error_type` は固定の低cardinality enumとし、user ID、run ID、session token、
IP、Cookie、Redis URL、例外本文を含めない。plan作成時の `RateLimitSignal`
（`missing_ip` / `unknown_write`）とRedis評価時の劣化は発生段階が異なるため、既存signalへ
無理に混在させず、専用のdegradation recorderとして扱う。backend capacityはRedis非依存なので、
capacity判定にfail-open / fail-closedの分岐を持たない。live Redisの読取不能は従来どおり503へ
fail-closedするが、回答生成やDB結果には影響させない。

## Attempt lifecycle contract（採用済み）

1接続は開始時にDBから得た1以上のattempt epochへ固定する。readerがより大きいepochを観測すると
`attempt_advanced` を返し、その境界entryは旧cursorで消費しない。SSE層は接続を閉じず、
`observed_attempt_epoch` へpinを更新し、返された `next_cursor` を維持して同じ接続で再読する。
境界非消費により、新epochの最初の残存entryから欠落なく配信できる。

DBのattempt取得transactionはRedis publishより先にcommitされるため、re-pinのためのDB再確認は
行わない。epochは連続している必要がなく、1から3への切替も許容する。re-pin後にさらに大きい
epochを観測した場合も同じ規則を繰り返す。`observed_attempt_epoch <= pinned_epoch` は内部契約違反
としてeventを送らず接続を閉じる。

小さいepochはゾンビworkerとしてskipし、cursorを進める。attempt帰属はStream IDやmarker位置
ではなくinteger epochの大小比較で決める。`attempt_advanced` 自体をSSE eventとして公開せず、
transportの6 eventだけを送る。

後続clientは `attempt.started` だけに境界判定を依存しない。6種類のどのeventでも、受信した
`attemptEpoch` が現在値より大きければ、表示中のdraftを破棄してepochを更新してからeventを
適用する。同じepochの重複markerではdraftを破棄しない。小さいepochはserverで除外するが、
clientも防御的に適用しない。

これによりmarkerがtrim、publish喪失、payload不正でskipされても、新epochの表示eventを旧draftへ
混ぜない。re-pin後に `ATTEMPT_ABSENT` が続く場合はcursorを進めながら有限時間待ち、上限または
最大接続時間で何も合成せずcloseする。

## Run state and degradation contract

| 状態 | response開始前 | 200開始後 |
|---|---|---|
| queued / `status = queued AND attempt_epoch = 0` | Redis probe成功後に200 | 2秒間隔・最大10秒の短命DB再確認で正のepochを待つ |
| completed / failed | 204 bodyなし | terminalを受信済みなら送信後close。未受信でもmax-age後の再接続で204 |
| `stream_missing` | 500ms間隔で2秒待っても不在なら503 + `Retry-After: 5` | 500ms間隔で2秒待っても不在ならsilent close |
| `attempt_absent` | 200 | 非terminalとして `next_cursor` から継続 |
| `empty` | 200 | 同じcursorからfollow継続 |
| `attempt_advanced` | observed epochへre-pinして再読 | 同一接続・同一cursorでre-pinして継続 |
| `cursor_trimmed` | 409 | silent close。次の自動再接続preflightで409 |
| `unavailable` | 503 + `Retry-After: 5` | 即時silent close。次の自動再接続で回復すれば200、継続不能なら503 |

後続UIが下書きを誤って継続しないことを優先し、不完全なreplayを成功扱いしない。
SSE独自のfallback通知は作らず、200 streamのclose後は同じEventSourceの自動再接続に任せる。
自動再接続中は `readyState = CONNECTING`、204 / 409 / 429 / 503を受けてfailした後は
`readyState = CLOSED` となる。後続UIは `error` 時にこの2状態を区別する。

`CONNECTING` 中は同じEventSourceが内部のLast-Event-IDを保持するため、別instanceを追加で
作らない。`CLOSED` 後は不完全なlive draftを表示継続せず、そのrunをpolling-onlyへ移す。
本sliceではmanual SSE retry用のquery cursorや永続cursorを追加しない。

## Baseline invariants

以下は親仕様・前提sliceから引き継ぎ、本sliceの実装とtestで固定する。

- Postgresがrun状態・会話・最終回答の唯一の正本である。
- Redis StreamまたはSSEの障害でrunをfailedにせず、DB状態を変更しない。
- 所有権確認が成功するまでRedisを読まない。
- 他者所有runと不在runは404に収束し、存在有無を漏らさない。
- admin roleもrun所有権条件を迂回しない。
- DB session / transactionをSSE接続中に保持しない。
- browserはprivate backendへ直接接続せず、同一origin BFFを通る。
- BFFのsession確認だけに依存せず、backendでもJWTとrun所有権を検証する。
- BFFは検証済みrun ID / cursorだけをupstream URL / headerへ埋め込む。
- eventのattempt帰属はStream IDやmarker位置ではなくinteger `attemptEpoch` の大小比較で決める。
- どの公開eventでもepoch増加をclientのdraft破棄境界とし、`attempt.started` の存在だけへ
  正しさを依存させない。
- Stream IDは再開cursorにだけ使い、DBへ保存しない。
- SSE dataは1行JSONとしてserializeし、本文のCR / LFでframe境界を作らせない。
- SSE層は既知の6 eventだけをallowlist投影し、未知eventをraw転送しない。
- terminalを送ったconsumerは、同じ接続で後続eventを表示用に送らない。
- terminal publishを失っても、全接続は最大接続時間内に終了する。
- terminal runへの新規・再接続は204で終了し、自動再接続を止める。
- SSE固有のcontrol eventやStream IDのない合成eventを送らない。
- heartbeatはcommentだけでありcursorを進めず、JavaScript側の沈黙検知には使わない。
- 200 streamの終了と非200 responseによるfailureを区別し、`CONNECTING` と `CLOSED` を
  同じ再接続状態として扱わない。
- `CLOSED` 後は同じrunでmanual SSE retryをせず、既存pollingへ固定する。
- payloadをlog、metric label、trace attribute、例外messageへ含めない。
- 既存run polling、`recentEvents`、thread詳細、最終回答表示は変更しない。
- SSE responseをcache・transform・圧縮・bufferしない。
- BFFのSSE接続開始は通常readと別bucketで数え、session+run 12/分、session 30/分、IP 120/分を
  利用可能な全tierで制限する。
- backendの同時接続は各API processでrun 2、user 4、process 50を原子的に強制する。
- process内counterを全instance共通の厳密な上限と表現しない。現在の運用上限はAPI Machine 2台、
  Uvicorn 1 process/Machineであり、全体100は運用上のceilingである。
- capacity slotはresponse開始前に取得し、全終了経路でresponse EOFより前に解放する。
- client切断でbackend / BFFの処理とRedis waitが有限時間内に終了する。
- disconnectはgenerator単体だけでなく、実際のmiddleware stackを通したapp全体で成立する。
- active接続数、接続時間、close理由をpayload非依存の低cardinality metricsで観測する。

## API compatibility

- additiveなGET endpoint追加であり、既存APIの破壊的変更ではない。
- 既存 `ResearchRunResponse` にSSE内部情報を追加しない。
- SSE event dataはDB modelやRedis raw envelopeを直接公開せず、型付きeventから投影する。
- FastAPI endpoint追加でOpenAPI outputが変わるため、実装sliceでは `/gen-types` を実行する。
- generated SDKがSSEの長時間streamingに適さない場合、BFFは専用fetchを使う。generated
  typeを無理にruntime clientとして再利用しない。

## Required file changes（想定）

```text
backend/app/agent/router.py
  # SSE endpoint、認証・owned context取得、StreamingResponse

backend/app/agent/runs/contracts.py
backend/app/agent/runs/repository.py
  # 公開schemaではないowned live context

backend/app/agent/live_updates/sse.py
  # SSE frame serializer、connection loop、heartbeat / close policy、process内capacity

backend/app/agent/live_updates/sse_response.py
  # body未開始を含むresponse lifecycleでcapacity leaseを冪等解放

backend/app/agent/live_updates/stream.py
  # follow方式やattempt切替決定に必要な最小拡張だけ

backend/tests/agent/test_router_research.py
backend/tests/agent/live_updates/test_sse.py
  # 認証・所有権・frame・cursor・heartbeat・disconnect

frontend/src/app/api/research/runs/[runId]/events/route.ts
frontend/src/app/api/research/runs/[runId]/events/route.node.test.ts
  # same-origin BFF proxy、session、header / body / abort中継

frontend/src/lib/proxy/rate-limit-plan.ts
frontend/src/lib/proxy/rate-limit-plan.node.test.ts
  # 通常readと分離したSSE接続開始bucket、session+run / session / IP tier

frontend/src/proxy.ts
frontend/src/proxy.node.test.ts
  # SSE routeを通常read bucketから除外する入口分類

frontend/src/lib/auth/rate-limit.ts
frontend/src/lib/auth/rate-limit.node.test.ts
frontend/src/lib/observability/server-log.ts
frontend/src/lib/observability/server-log.node.test.ts
  # Redis fail-openのrequest class別metricと固定event log

frontend/src/types/*
  # OpenAPI更新に伴い /gen-types で必要な場合だけ更新
```

配置は保証条件レビュー後に確定する。SSE loopをrouterへ直接詰め込まず、HTTPに依存しない
frame / lifecycle部分を `live_updates` に置く候補とする。

## Tests（骨格）

確定済み契約を次の分類でtestへ対応させる。公開shapeと具体値はすべて確定済みである。

### Backend contract

- JWTなし・不正JWTは401。
- 不在runと他者所有runは404であり、Redis readerを呼ばない。
- admin JWTでも他者所有runは404であり、所有権条件を迂回しない。
- malformed `Last-Event-ID` は400であり、Redis readerを呼ばない。
- terminal runは204 bodyなしであり、Redis readerを呼ばない。
- trim済みcursorは409であり、不完全なreplayを200として開始しない。
- 初回availability window後の `STREAM_MISSING` と `UNAVAILABLE` は503 + `Retry-After` になる。
- ownerだけが200 `text/event-stream` を開始できる。
- queued runはepoch 0をreaderへ渡さず、Redis probe成功後に200を開始する。
- responseが `no-store, no-transform` であり、buffering抑止headerが付く。
- 同じprocessで同一runの1・2接続目は開始でき、3接続目は429 + `Retry-After: 5` になる。
- 同じprocessで同一userが異なるrunへ張る1〜4接続目は開始でき、5接続目は429になる。
- 同じprocessの1〜50接続目はcapacityを取得でき、51接続目はDBへ触れず503になる。
- 不在run・他者所有run・terminal run・run/user上限拒否でprocess slotが残らない。
- 所有権確認前にrun / user keyed counterを作らず、capacity拒否時もRedis readerを呼ばない。

### SSE protocol

- Redis Stream ID、event type、JSON dataが正確なframeになる。
- `\r`、`\n`、`\r\n`、NUL、quote、backslash、Unicodeを含むtextでも、literal制御文字を
  `data:` 行へ出さず、JSON round-trip後に元本文へ戻る。
- 本文が行頭 `id:` / `event: terminal` / `data:` / `retry:` を含んでも、受信側では
  1件の元eventとしてparseされ、event ID / type / retry時間を変更しない。
- 未知event typeをSSEへ投影せず、raw payloadを送らない。
- 未知event破棄を `reason = unknown_event` の固定counterで観測し、type値やpayloadをlabelへ
  含めない。
- transportの6 event以外のcontrol eventを投影せず、全公開eventがRedis Stream IDを持つ。
- activityは `{ attemptEpoch, activity: { type, ... } }` としてnested投影し、JSON fieldの
  `event`、activity固有fieldのtop-level flatten、snake_caseの公開fieldを出さない。
- frontendが `payload.activity.type` で既知の `AnswerProgressEvent` をdiscriminateでき、
  未知type・余分なfield・旧 `event` 形式をraw転送しない。
- 最初のeventまたは待機より前に `retry: 1000` を送り、MessageEventやcursor更新を発生させない。
- `Last-Event-ID` の直後から順序通りreplayする。
- heartbeat commentがevent IDを持たずcursorを進めず、MessageEventとしてdispatchされない。
- terminal frame後に接続を閉じ、後続entryを送らない。
- payloadや認証情報をlogへ出さない。

### Lifecycle and degradation

- queuedからepoch 1以上へ進むと、同じ接続でそのepochのRedis replay / followへ移る。
- queued DB再確認が2秒間隔・最大10秒で、readやsessionを重ねず停止する。
- queued待機中のterminal化、DB再確認失敗、待機上限、client切断が規定どおりclose / cancelされ、
  DB sessionを待機中に保持しない。
- `ATTEMPT_ADVANCED` でcursorを境界前に維持し、DBを再取得せず同一接続でobserved epochへ
  re-pinして境界entryを欠落なく配信する。
- epochが飛んだ場合と、re-pin後にさらに大きいepochを観測した場合も同じ接続で切り替わる。
- validな `attempt.started` がtrim・decode不良で存在しなくても、最初に届く任意eventの
  `attemptEpoch` 増加により旧draftへ混ぜないclient契約になっている。
- re-pin後の `ATTEMPT_ABSENT` が連続しても、有限時間で接続を閉じる。
- 初回 / mid-streamの2秒grace、500ms follow、10秒heartbeat、45秒max ageが、event受信や
  re-pinでdeadlineを延長せず規定どおり動く。
- follow中のゾンビのみbatchは `next_cursor` から継続し、劣化や切断として扱わない。
- 200開始後のRedis障害ではeventを合成せずcloseし、同じEventSourceの自動再接続preflightが
  200なら継続、503なら `CLOSED` へ収束する。
- 200 streamのcloseでは `CONNECTING`、204 / 409 / 429 / 503では `CLOSED` になる契約を
  browser testで固定する。
- max age closeと同epochの再接続ではdraft、current epoch、適用済みIDを維持し、ちらつき・
  loading表示への後退・deltaの二重適用がない。
- max age再接続中に大きいepochを受信した場合だけdraftを1回破棄する。
- terminal publishを失っても最大接続時間で接続が終了し、再接続preflightがDB terminalを
  検出して204を返す。
- client disconnectでgenerator / Redis readがcancelされる。
- terminal、max age、client disconnect、BFF abort、Redis error、generator exception、
  cancellationの各経路でcapacity slotを1回だけ解放する。
- response start失敗でbody iteratorが一度も開始されなくてもcapacity slotを解放し、
  response ownership自体に到達しなかったleaseも55秒後の次回取得で除去する。
- backendはmax ageのresponse EOFより前にslotを解放し、`retry: 1000` の再接続が古いslotにより
  偽の429へならない。
- 新しいcapacity allocatorを作るとcounterが空で開始し、process crash後に永続したslotや
  decrement漏れを引き継がない。
- `SecurityHeadersMiddleware` と実際のmiddleware stackを含むapp全体でも、client disconnectが
  generator / Redis waitへ伝播する。
- 複数接続が同じrun eventを独立して読める。
- 同時接続上限の超過が、確定したcapacity契約どおりresponse開始前に拒否される。
- SSE失敗がrun statusや最終DB結果を変更しない。

### BFF

- SSE routeが通常read bucketを消費せず、session+run 12/分、session 30/分、IP 120/分の
  利用可能な全tierを同時に評価する。
- 同じsessionでrun IDを変えてもsession全体tier、sessionを変えてもIP tierで上限が効く。
- BFFのSSE接続開始上限超過はupstream接続前に429となり、limiter算出の `Retry-After` が付く。
- path traversal、encoded slash、CR / LF、過大値を含むmalformed run ID / cursorを
  upstream接続前に拒否し、upstream fetchを一度も呼ばない。
- sessionなしは401でupstreamへ接続しない。
- sessionなしの連打も未検証cookie token + IPの先行tierでsession store read前に制限される。
- 大文字UUIDを含む同一run表現は小文字canonical keyへ収束し、per-run上限を分割しない。
- user JWT、`Accept: text/event-stream`、`Last-Event-ID`を正しく転送する。
- upstream bodyをbuffer・JSON変換せずstreamとして返す。
- 既存15秒timeoutで接続を切らず、backendの45秒deadlineより長い50秒hard timeoutでabortする。
- browser abortをupstreamへ伝播する。
- browser abortは未処理例外やerror logにせず終了し、hard timeout / network failureは503へ
  収束する。
- backendの204をbodyなしで中継する。
- backendの409 / 429 / 503 / `Retry-After` をbody変換せず中継する。
- `Cache-Control: no-store, no-transform` を保持し、SSEへ `Content-Encoding` を付けない。
- 公開responseにも `X-Accel-Buffering: no` を付ける。
- backendの認証・認可・劣化statusを、確定した契約どおりno-storeで中継する。
- rate-limit Redis未設定・接続失敗・eval失敗ではrequestをfail-openで許可し、
  `request_class="sse"` のcounterと固定event logを記録する。
- fail-open logは60秒throttleされ、user / run / session / IP / Cookie / Redis URL / 例外本文を
  metric labelまたはlog fieldへ含めない。

### Integration

- 実Redisへ合成eventをpublishし、FastAPI SSEから同じID・順序・payloadで取得できる。
- 保持済みeventを途中cursorからreplayできる。
- heartbeat待機中の切断でRedis / DB resourceが残らない。
- middlewareを含む実appへの接続を切断し、generator / Redis waitが有限時間内に終了する。
- production buildを `next start` で起動し、`Accept-Encoding: gzip` を付けてもSSEが圧縮されず、
  heartbeat / eventが接続終了前に逐次到着する。
- Redis停止・遅延時もrun DB stateが変わらない。
- mid-stream close後の自動再接続がLast-Event-IDを維持し、preflight 200なら重複なく再開する。
- `retry: 1000` によりmax age後の再接続を約1秒で開始し、user agentの追加backoffを許容する。
- preflight 503でnative EventSourceが `CLOSED` になり、同じrunで新instanceを自動生成しない。
- 同じrunの3枚目のtabがbackend 429を受けると `CLOSED` になり、polling-onlyへ劣化する。
- capacityはprocess単位であり、独立した2 processではrun / user counterを共有しないことを
  明示的にtestし、このsliceがdistributed上限を保証しないことを固定する。

## Verification

- backend変更後に `/check` を実行する。
- frontend変更後にBiome、TypeScript、対象Vitestを実行する。
- FastAPI OpenAPI outputを確認し、`/gen-types` を実行する。
- backend integration環境でPostgres / Redisを使うSSE testを実行する。
- generator単体に加え、`SecurityHeadersMiddleware` を含むapp全体でdisconnectを検証する。
- `next dev` だけでなくproduction build + `next start` で圧縮・bufferingを検証する。
- payload、JWT、Cookie、例外本文がtest failure outputやstructured logへ出ていないことを確認する。
- metricsが固定close reasonだけを使い、user / run / cursor / payloadをlabelに持たないことを確認する。
- BFF fail-open counterが固定request classだけを、capacity拒否counterが固定scopeだけをlabelに
  持つことを確認する。
- `backend/fly.core.toml` がAPI MachineごとにUvicorn 1 processを起動することを確認し、deploy時の
  API Machine上限2台をoperational checkへ追加する。
- production buffering / idle timeout / ACL / 実負荷での接続上限確認はoperational sliceへ残す。

## Risks and impact estimate

| 項目 | 影響 | 軽減策 |
|---|---|---|
| 長時間HTTP接続 | backend / BFFのconnectionとtaskを保持する | heartbeat、abort伝播、最大接続時間を契約化 |
| terminal publish喪失 | DB完了後もheartbeatを無限送信する | 最大接続時間と再接続時DB確認、terminal runへの204 |
| frame injection | LLM本文が偽terminal / ID / retryを作る | 1行JSON escape、server生成field、CR単体を含むtest |
| Redis pool枯渇 | SSE readが通常Redis操作を妨げる | non-blocking 500ms read、500ms timeout、capacity上限で制御 |
| 接続数枯渇 | request rate limitだけではopen connection数を抑えられない | BFF開始頻度とbackendのrun 2 / user 4 / process 50上限 |
| process間の上限乖離 | run / user上限は別processと共有されない | 1 process/Machine・API Machine最大2を運用制約にし、拡張前にdistributed leaseを再検討 |
| counter解放漏れ | 正常終了後もslotが残り、偽の429 / 503になる | 全終了経路の `finally` releaseとresponse EOF前の解放をtest |
| BFF limiter劣化 | Redis障害で接続開始rate limitがfail-openする | backend最終上限、request class別counter、固定event log |
| DB pool枯渇 | yield dependencyが接続終了までsessionを保持し得る | stream開始前にDTO化してsessionを閉じる |
| attempt再配送race | 旧worker eventが下書きへ混ざる | 同一接続re-pinと全eventのepoch比較を必須test化 |
| proxy buffering | eventがリアルタイムに届かない | no-buffer headerとoperational検証 |
| Next.js既定圧縮 | gzipが小さいeventをbufferする | `no-transform` とproduction `next start` test |
| EventSource再接続loop | 劣化状態で無限再接続する | 200 closeは同instance、非200後はpolling-onlyに固定 |
| 高頻度再接続 | `retry: 1000` が障害時の接続開始数を増やす | 1 clientあたり約1 request/秒をcapacity設計へ織り込む |
| max age時の表示回帰 | 45秒ごとにdraftが消える・重複する | Last-Event-IDと同epoch維持をbrowser testで固定 |
| marker欠落 | 新epochのeventが旧draftへ混ざる | `attempt.started` だけでなく全eventのepoch増加でdraftを破棄 |
| heartbeat不可視 | browserがcomment受信時刻を監視できない | commentは接続維持専用とし、livenessはpollingを正本にする |
| payload漏洩 | 回答下書きがlogやerrorへ残る | body非loggingとredaction test |
| 既存UI回帰 | polling表示が変わる | このsliceではUIと既存run responseを変更しない |

## Implementation order（Draft）

1. transportのactivity fieldを `event` から `activity` へ変更し、strict decode testを更新する。
2. owned run live contextとrepository testを追加する。
3. process内capacity allocatorとrelease testを追加する。
4. SSE frame serializerとconnection lifecycleをtest firstで実装する。
5. FastAPI endpointの認証・所有権・cursor・capacity契約を実装する。
6. BFF専用SSE rate-limit planとfail-open観測を実装する。
7. BFF Route Handlerとstream / abort testを実装する。
8. 実Redisを使うbackend integration testを追加する。
9. OpenAPI / generated typesを同期する。
10. backend / frontend verificationを実行する。

## Done（Draft）

- ownerだけがBFF経由でrunのSSE接続を開始できる。
- 不在・他者所有runはRedisを読む前に404となる。
- adminも他者所有runへ接続できない。
- LLM本文、run ID、cursor、未知eventがSSE frame / upstream requestを偽造できない。
- 合成eventをRedis StreamからSSE frameへ変換し、cursorから再開できる。
- heartbeat、terminal close、最大接続時間、client disconnect cleanupが自動testで確認される。
- non-blocking follow 500ms、heartbeat 10秒、max age 45秒、BFF timeout 50秒、各2秒grace、
  queued 2秒間隔・10秒上限、`Retry-After: 5` が設定可能な定数として実装されている。
- `retry: 1000` がeventより先に送られ、max age後の定常再接続でdraftを維持できる。
- terminal runへの新規・再接続が204で停止する。
- queued runが200で有限時間待機し、短命DB再確認からrunningの正のepochへ移れる。
- `ATTEMPT_ADVANCED` が同一接続・境界前cursorのままre-pinされ、新epoch eventを欠落させない。
- SSE固有control eventを追加せず、全eventのepoch増加をdraft破棄境界として扱える。
- activityが `{ attemptEpoch, activity: AnswerProgressEvent }` としてnested・camelCaseで公開され、
  `event` fieldやtop-level flattenを公開契約に残さない。
- `STREAM_MISSING` / `UNAVAILABLE` は503、`CURSOR_TRIMMED` は409へ収束し、
  mid-stream障害はsilent closeになる。
- 200 stream終了後の `CONNECTING` と非200後の `CLOSED` が区別され、`CLOSED` 後は
  polling-onlyへ移る。
- BFFが15秒timeout、圧縮、body bufferingを挟まず、認証付きSSEを中継できる。
- 未認証requestがsession store前のSSE専用rate limitを迂回できない。
- Redis / SSE障害がrun状態、最終DB結果、既存pollingを変更しない。
- BFFがSSE接続開始をsession+run 12/分、session 30/分、IP 120/分で通常readと別に制限する。
- backendが各API processでrun 2、user 4、process 50の同時接続上限を強制し、429 / 503を
  response開始前に返す。
- 全終了経路でcapacity slotがresponse EOF前に解放され、process crash後に永続slotが残らない。
- body iterator未開始でもresponse finalizerがleaseを解放し、最終backstopの55秒janitorで
  ghost leaseから自己回復できる。
- BFF limiterのRedis fail-openとbackend capacity拒否をPIIなしの固定metric / logで観測できる。
- process内上限をglobal保証とせず、1 process/Machine・API Machine最大2の運用前提が明記される。
- active接続数・接続時間・close理由を安全に観測できる。
- attempt変更と各劣化状態が本仕様の契約どおりに扱われる。
- UI、Gemini streaming、producer接続、DB schemaを変更していない。
- backend / frontend checksと対象integration testがgreenである。

## 実装前判断の完了

attempt切替、queued、劣化通知、HTTP 200開始境界、control event不採用、native EventSourceの
再接続規約、follow方式、全時間値、BFF接続開始rate limit、backend同時接続capacity、activityの
nested shapeと `activity` field名は確定済みである。本仕様内に未決の設計事項は残っていない。
