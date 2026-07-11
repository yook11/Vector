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

本書は初期Draftである。認証・所有権・protocol integrity・接続の有限終了・圧縮防止は
保証条件として採用済みである。一方、「接続中のattempt切替」「queued runへの接続」
「Redis劣化状態の通知方法」「接続数制限の実装機構」「heartbeat / poolの具体値」は
実装前の要判断事項とする。

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
  開いたままのSSE接続数は制限しない。接続時間と同時接続数には別の上限が必要である。
- `next start` は既定でresponse圧縮を有効にし、現行 `next.config.js` は
  `compress: false` を設定していない。SSE Route Handler自身が変換・bufferingを防ぐ必要がある。
- backendにはGZip middlewareがないが、`SecurityHeadersMiddleware` は
  `BaseHTTPMiddleware` を継承する。disconnect保証はgenerator単体だけでなくapp全体で
  検証する必要がある。
- FastAPIのyield dependencyで開いたDB sessionはstreaming responseの終了までcleanupが
  遅れる可能性がある。SSE loop中にDB session / transactionを保持してはならない。

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
- disconnectをBFF、middleware、backend generator、Redis waitまで伝播する。
- SSE responseをcache・圧縮・bufferしない。
- 同時接続数を有限にする。実装場所・計数方式・上限値は要判断とする。

### P2: observability

- active接続数、接続時間、close理由を観測する。
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

### Out of scope

- Geminiのstreaming APIへの切替。
- provider chunkの集約、delta coalescing、generation管理。
- `stage` / `activity` をRedis Stream publisherへ接続すること。
- `answer.delta` / `answer.reset` / `terminal` のproducer接続。
- cancel endpointからterminal eventをpublishすること。
- browser `EventSource`、React hook、下書きstate、画面表示。
- SSE失敗時のpolling fallback UI。
- citation、sources、missing aspectsの途中表示。
- Redis List / `recentEvents` の変更・削除。
- DB schema / migration、永続cursor、下書き保存。
- Fly / CDNの実環境検証とproduction負荷試験。これらはoperational sliceで扱う。
- WebSocket、consumer group、consumer ack、exactly-once配信。
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

成功response候補:

```text
status: 200
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-store, no-transform
X-Accel-Buffering: no
```

`Connection` はHTTP/2やproxyでhop-by-hopに扱われるため、公開契約の必須headerには
しない。production proxyでbufferingされないことはoperational sliceで確認する。

response statusの確定事項:

| status | 条件 |
|---|---|
| 400 | `Last-Event-ID` が許可するStream ID形式でない |
| 401 | BFF JWTがない、期限切れ、署名・issuer・audienceが不正 |
| 404 | runが存在しない、または現在ユーザーの所有物でない |
| 204 | runが既に `completed` / `failed` であり、最終DB結果へ移るべき |

204はEventSourceの自動再接続を停止する終端responseである。BFFはbodyを追加せず204を
そのままbrowserへ中継する。

queued run、Redis初期read失敗、trim済みcursorのstatusは「要判断」に残す。
response開始後の障害はHTTP statusを変更できないため、closeだけで表すかSSE control eventを
追加するかを保証条件レビューで決める。

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
   upstream URL / headerへ埋め込む。
2. Better Auth sessionがなければupstreamへ接続せず401を返す。
3. sessionから内部JWTを発行し、Authorization headerとしてbackendへ送る。
4. browserからの `Last-Event-ID` を値を変えずbackendへ転送する。
5. upstreamのSSE bodyをJSON parse・再serializeせず、byte streamとして返す。
6. request abort時にupstream fetchとbody readをcancelする。
7. SSE専用fetchには既存の15秒timeoutを適用しない。
8. payload、Authorization、Cookie、response bodyをlogへ出さない。
9. responseに `Cache-Control: no-store, no-transform` を設定し、SSE bodyを圧縮・bufferしない。
10. backendの204をbodyなしで中継し、terminal runへの自動再接続を停止する。

`runId` 検証はupstream URLへのpath injection、`Last-Event-ID` 検証はCR / LFを使った
header injectionを防ぐ境界である。malformed値はupstream fetch前に400へ収束させる。
Fetch / Headers実装が独自に拒否することへ依存せず、application contractとして検証する。

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

```

候補event:

| SSE event | data |
|---|---|
| `attempt.started` | `attemptEpoch` |
| `stage` | `attemptEpoch`, `stage` |
| `activity` | `attemptEpoch`, safe activity payload |
| `answer.delta` | `attemptEpoch`, `generation`, `text` |
| `answer.reset` | `attemptEpoch`, `generation` |
| `terminal` | `attemptEpoch`, `status`, optional `errorCode` |

`activity` のsafe payloadを `event` fieldへnestedで載せるか、SSE dataへflattenするかは
親仕様と現在のPydantic event modelで表現が揃っていないため要判断とする。

Redis内部の `publishedAt` とraw envelopeはSSEへ公開しない。質問、prompt、会話文脈、
chain of thought、未選別evidence、provider生response、secret、例外本文も送らない。

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

heartbeatはSSE comment frameを候補とする。

```text
: heartbeat

```

heartbeatにevent IDを付けず、再開cursorを進めない。全接続に有限の最大接続時間を設け、
BFF JWTの60秒TTLより短い時間で一度終了・再認証させる。具体的なheartbeat間隔と
最大接続時間は要判断とする。

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
2. DBでowned run live contextを読む。
3. DB sessionを閉じる。
4. 初回Redis readを行う。
5. 200 responseを開始し、保持済みeventをID順に送る。
6. cursorへ追いついた後は、新規eventを待ちながらheartbeatを送る。
7. terminal eventを送った場合は、それより後のentryを送らず接続を閉じる。
8. client disconnect / task cancellation時はRedis waitをcancelし資源を解放する。
9. terminalを受信できなくても最大接続時間で接続を閉じる。
10. 再接続時にBetter Auth sessionとDB statusを再確認し、terminal runなら204を返す。

初回readをresponse開始前に行うか、200開始後に行うかは、Redis劣化状態をHTTP statusで
表せる範囲に影響するため要判断とする。

terminal publishはbest-effortであり、streamにterminalが無いままDBだけがterminalに
なることを許容する。初期実装では接続中のDB定期pollingを追加せず、最大接続時間を
backstopにする。最大時間後の再接続preflightでDB statusを読み、completed / failedなら
204でEventSourceを停止する。これによりDB sessionを長時間保持せず、接続も無限化しない。

接続lifecycleは次の低cardinality metricsで観測する。名称は既存metrics規約に合わせて
実装時に確定するが、意味は固定する。

```text
active SSE connections gauge
SSE connection duration histogram
SSE close total counter { reason }
```

`reason` は `terminal` / `max_age` / `unavailable` / `client_disconnect` /
`cursor_trimmed` / `capacity_rejected` などの固定enumだけを許可する。

### Redis follow

既存readerは短時間のnon-blocking readである。SSE追従方法には次の候補がある。

1. 専用Redis poolで有限時間の `XREAD BLOCK` を行い、timeoutごとにheartbeatする。
2. non-blocking readを短いintervalで繰り返す。

共有Redis poolを長時間blocking readで占有する構成は採用しない。接続上限、pool size、
idle時のcommand数、cancel時の挙動を比較して保証条件レビューで方式を確定する。

### Connection capacity（要判断）

通常のrequest rate limitは接続開始回数を抑えるが、開いた接続の同時数を制限しない。
backend / BFF / Redis poolの資源を守るため、SSE同時接続数は有限にする。

次を実装前に決める。

- 最終強制をBFFとbackendのどちらに置くか。backendを最終境界とする案を優先する。
- user単位、run単位、backend instance単位、全体のどの上限を持つか。
- multi-instanceで正確に共有するか、instanceごとの保護に限定するか。
- Redis counter / leaseを使う場合のatomic acquire / releaseとcrash回復用TTL。
- 上限超過時にresponse開始前の429と `Retry-After` を返すか。
- Redis障害時にcapacity checkをfail-open / fail-closedのどちらにするか。

process内counterだけで全instance共通上限を保証したことにしない。Redis leaseを使う場合は
最大接続時間より長いTTLを必須にし、`finally` releaseに失敗しても有限時間で回復させる。

## Attempt lifecycle contract（採用済み）

1接続は開始時にDBから得た1以上のattempt epochへ固定する。readerがより大きいepochを観測すると
`attempt_advanced` を返し、その境界entryは旧cursorで消費しない。SSE層は接続を終了して所有権を
再確認し、DBから新しいepochを取得したうえでcursorなしで再接続する。これにより新attemptの
markerを含む保持済みeventを欠落なくreplayする。

小さいepochはゾンビworkerとしてskipし、cursorを進める。attempt帰属はStream IDやmarker位置
ではなくinteger epochの大小比較で決める。`attempt_advanced` をSSE control eventとして送るか、
HTTP接続closeだけで表すかは、このsliceのresponse lifecycle判断として残す。

## Run state and degradation contract

次の状態について、backend status、SSE control event、BFF pass-through、後続UIの動作を
一つの表として確定する必要がある。

| 状態 | 現在分かっていること | 未決定事項 |
|---|---|---|
| queued / `status = queued AND attempt_epoch = 0` | readerの入力条件を満たさない | 204、409、待機のどれにするか |
| completed / failedで新規接続 | 最終DB結果が正本 | 204でbodyなし終了し、EventSource再接続を停止する |
| `stream_missing` | TTL切れまたは未作成 | retry可能状態かpolling固定劣化か |
| `attempt_absent` | keyはあるが現epoch eventなし | 非terminalとして `next_cursor` から継続し、初回も最大接続時間内で待つ |
| `attempt_advanced` | より新しいattemptを観測 | DB再取得後にcursorなしで再接続する通知方法 |
| `cursor_trimmed` | 完全な下書き再構成不可 | reset controlを送るかHTTPで拒否するか |
| `unavailable` | Redis例外またはtimeout | 503、control event、接続closeの組合せ |
| 接続中のRedis障害 | 既にHTTP 200開始済み | clientへ理由を伝えるeventが必要か |

後続UIが下書きを誤って継続しないことを優先し、不完全なreplayを成功扱いしない。

## Baseline invariants

以下は親仕様・前提sliceから引き継ぐ。詳細な保証条件とtestの対応は次のレビューで増補する。

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
- Stream IDは再開cursorにだけ使い、DBへ保存しない。
- SSE dataは1行JSONとしてserializeし、本文のCR / LFでframe境界を作らせない。
- SSE層は既知の6 eventだけをallowlist投影し、未知eventをraw転送しない。
- terminalを送ったconsumerは、同じ接続で後続eventを表示用に送らない。
- terminal publishを失っても、全接続は最大接続時間内に終了する。
- terminal runへの新規・再接続は204で終了し、自動再接続を止める。
- heartbeatはcursorを進めない。
- payloadをlog、metric label、trace attribute、例外messageへ含めない。
- 既存run polling、`recentEvents`、thread詳細、最終回答表示は変更しない。
- SSE responseをcache・transform・圧縮・bufferしない。
- 同時接続数を有限にする。具体的な上限・計数方式は実装前に確定する。
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
  # SSE frame serializer、connection loop、heartbeat / close policy

backend/app/agent/live_updates/stream.py
  # follow方式やattempt切替決定に必要な最小拡張だけ

backend/tests/agent/test_router_research.py
backend/tests/agent/live_updates/test_sse.py
  # 認証・所有権・frame・cursor・heartbeat・disconnect

frontend/src/app/api/research/runs/[runId]/events/route.ts
frontend/src/app/api/research/runs/[runId]/events/route.node.test.ts
  # same-origin BFF proxy、session、header / body / abort中継

frontend/src/types/*
  # OpenAPI更新に伴い /gen-types で必要な場合だけ更新
```

配置は保証条件レビュー後に確定する。SSE loopをrouterへ直接詰め込まず、HTTPに依存しない
frame / lifecycle部分を `live_updates` に置く候補とする。

## Tests（骨格）

具体的な期待値は保証条件レビュー後に固定する。少なくとも次の分類を持つ。

### Backend contract

- JWTなし・不正JWTは401。
- 不在runと他者所有runは404であり、Redis readerを呼ばない。
- admin JWTでも他者所有runは404であり、所有権条件を迂回しない。
- malformed `Last-Event-ID` は400であり、Redis readerを呼ばない。
- terminal runは204 bodyなしであり、Redis readerを呼ばない。
- ownerだけが200 `text/event-stream` を開始できる。
- responseが `no-store, no-transform` であり、buffering抑止headerが付く。

### SSE protocol

- Redis Stream ID、event type、JSON dataが正確なframeになる。
- `\r`、`\n`、`\r\n`、NUL、quote、backslash、Unicodeを含むtextでも、literal制御文字を
  `data:` 行へ出さず、JSON round-trip後に元本文へ戻る。
- 本文が行頭 `id:` / `event: terminal` / `data:` / `retry:` を含んでも、受信側では
  1件の元eventとしてparseされ、event ID / type / retry時間を変更しない。
- 未知event typeをSSEへ投影せず、raw payloadを送らない。
- `Last-Event-ID` の直後から順序通りreplayする。
- heartbeatがevent IDを持たずcursorを進めない。
- terminal frame後に接続を閉じ、後続entryを送らない。
- payloadや認証情報をlogへ出さない。

### Lifecycle and degradation

- queued、terminal、stream missing、attempt absent、cursor trimmed、Redis unavailableが、
  確定した結果契約どおりに区別される。
- 接続中のattempt変更で旧attempt eventを新attemptの下書きへ混ぜない。
- terminal publishを失っても最大接続時間で接続が終了し、再接続preflightがDB terminalを
  検出して204を返す。
- client disconnectでgenerator / Redis readがcancelされる。
- `SecurityHeadersMiddleware` と実際のmiddleware stackを含むapp全体でも、client disconnectが
  generator / Redis waitへ伝播する。
- 複数接続が同じrun eventを独立して読める。
- 同時接続上限の超過が、確定したcapacity契約どおりresponse開始前に拒否される。
- SSE失敗がrun statusや最終DB結果を変更しない。

### BFF

- path traversal、encoded slash、CR / LF、過大値を含むmalformed run ID / cursorを
  upstream接続前に拒否し、upstream fetchを一度も呼ばない。
- sessionなしは401でupstreamへ接続しない。
- user JWT、`Accept: text/event-stream`、`Last-Event-ID`を正しく転送する。
- upstream bodyをbuffer・JSON変換せずstreamとして返す。
- 既存15秒timeoutで接続を切らない。
- browser abortをupstreamへ伝播する。
- backendの204をbodyなしで中継する。
- `Cache-Control: no-store, no-transform` を保持し、SSEへ `Content-Encoding` を付けない。
- backendの認証・認可・劣化statusを、確定した契約どおりno-storeで中継する。

### Integration

- 実Redisへ合成eventをpublishし、FastAPI SSEから同じID・順序・payloadで取得できる。
- 保持済みeventを途中cursorからreplayできる。
- heartbeat待機中の切断でRedis / DB resourceが残らない。
- middlewareを含む実appへの接続を切断し、generator / Redis waitが有限時間内に終了する。
- production buildを `next start` で起動し、`Accept-Encoding: gzip` を付けてもSSEが圧縮されず、
  heartbeat / eventが接続終了前に逐次到着する。
- Redis停止・遅延時もrun DB stateが変わらない。

## Verification

- backend変更後に `/check` を実行する。
- frontend変更後にBiome、TypeScript、対象Vitestを実行する。
- FastAPI OpenAPI outputを確認し、`/gen-types` を実行する。
- backend integration環境でPostgres / Redisを使うSSE testを実行する。
- generator単体に加え、`SecurityHeadersMiddleware` を含むapp全体でdisconnectを検証する。
- `next dev` だけでなくproduction build + `next start` で圧縮・bufferingを検証する。
- payload、JWT、Cookie、例外本文がtest failure outputやstructured logへ出ていないことを確認する。
- metricsが固定close reasonだけを使い、user / run / cursor / payloadをlabelに持たないことを確認する。
- production buffering / idle timeout / ACL / connection上限の実環境確認はoperational sliceへ残す。

## Risks and impact estimate

| 項目 | 影響 | 軽減策 |
|---|---|---|
| 長時間HTTP接続 | backend / BFFのconnectionとtaskを保持する | heartbeat、abort伝播、最大接続時間を契約化 |
| terminal publish喪失 | DB完了後もheartbeatを無限送信する | 最大接続時間と再接続時DB確認、terminal runへの204 |
| frame injection | LLM本文が偽terminal / ID / retryを作る | 1行JSON escape、server生成field、CR単体を含むtest |
| Redis pool枯渇 | blocking readが通常Redis操作を妨げる | 専用poolまたはnon-blocking方式を比較して固定 |
| 接続数枯渇 | request rate limitだけではopen connection数を抑えられない | backend最終上限、必要ならTTL付きdistributed lease |
| DB pool枯渇 | yield dependencyが接続終了までsessionを保持し得る | stream開始前にDTO化してsessionを閉じる |
| attempt再配送race | 旧worker eventが下書きへ混ざる | epoch境界と再接続規約を必須test化 |
| proxy buffering | eventがリアルタイムに届かない | no-buffer headerとoperational検証 |
| Next.js既定圧縮 | gzipが小さいeventをbufferする | `no-transform` とproduction `next start` test |
| EventSource再接続loop | 劣化状態で無限再接続する | HTTP / control event / retry規約を確定 |
| payload漏洩 | 回答下書きがlogやerrorへ残る | body非loggingとredaction test |
| 既存UI回帰 | polling表示が変わる | このsliceではUIと既存run responseを変更しない |

## Implementation order（Draft）

1. 本仕様の要判断事項と保証条件を確定する。
2. owned run live contextとrepository testを追加する。
3. SSE frame serializerとconnection lifecycleをtest firstで実装する。
4. FastAPI endpointの認証・所有権・cursor契約を実装する。
5. BFF Route Handlerとstream / abort testを実装する。
6. 実Redisを使うbackend integration testを追加する。
7. OpenAPI / generated typesを同期する。
8. backend / frontend verificationを実行する。

## Done（Draft）

- ownerだけがBFF経由でrunのSSE接続を開始できる。
- 不在・他者所有runはRedisを読む前に404となる。
- adminも他者所有runへ接続できない。
- LLM本文、run ID、cursor、未知eventがSSE frame / upstream requestを偽造できない。
- 合成eventをRedis StreamからSSE frameへ変換し、cursorから再開できる。
- heartbeat、terminal close、最大接続時間、client disconnect cleanupが自動testで確認される。
- terminal runへの新規・再接続が204で停止する。
- BFFが15秒timeout、圧縮、body bufferingを挟まず、認証付きSSEを中継できる。
- Redis / SSE障害がrun状態、最終DB結果、既存pollingを変更しない。
- 同時接続数が有限であり、active接続数・接続時間・close理由を安全に観測できる。
- attempt変更と各劣化状態が、次の保証条件レビューで確定した契約どおりに扱われる。
- UI、Gemini streaming、producer接続、DB schemaを変更していない。
- backend / frontend checksと対象integration testがgreenである。

## 次に決める保証条件

次のレビューでは、実装へ進む前に以下を期待値とtest caseの組で確定する。

`attempt_absent` は非terminalとして接続継続する契約に確定済みであり、以下の劣化通知判断には
含めない。

1. 接続中のattempt切替と旧worker遅延eventの扱い。
2. queued runへ新規接続した場合のresponse。completed / failedは204で確定済み。
3. `stream_missing` / `cursor_trimmed` / `unavailable` の通知方法。
4. 初回read前後でHTTP 200を開始する境界。
5. heartbeat、blocking read、専用pool、接続最大時間の具体値。
6. SSE control eventを追加するか、HTTP statusとcloseだけで表現するか。
7. `activity` dataのnested / flat shape。
8. BFFとbackendのabort・timeout・再接続規約。
9. 接続数制限の実装場所、単位、上限、distributed lease、TTL、429契約。
10. capacity checkのRedis障害時にfail-open / fail-closedのどちらを採るか。
