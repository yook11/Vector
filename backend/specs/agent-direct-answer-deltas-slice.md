# Agent Direct answer delta streaming slice 仕様

Status: Implemented — 2026-07-12

`agent-evidence-answer-draft-deltas-slice.md`の実装により、`AnswerVisibleTextFilter`と
`AnswerGenerationStopped`は共有answering位置へ昇格済みである。`generation`は共通の表示revisionであり、
Direct pathでは引き続きprovider attempt number 1〜2と一致する。共有化後もDirect固有のmarker / outer
whitespace、retry、finalizationの意味論は変更しない。

親仕様: `agent-answer-streaming-sse.md`。

前提slice:

- `question-answering-direct-answer-slice.md`
- `agent-live-stream-transport-slice.md`
- `agent-attempt-epoch-fencing-token-slice.md`
- `agent-sse-backend-bff-slice.md`
- `agent-live-event-producer-wiring-slice.md`

後続slice:

- Research UI
- Operational verification

## Positioning

本sliceは、plain textで生成するDirect answerをGeminiのasync streaming APIへ切り替え、
生成途中の安全な本文断片を既存Redis Streamの`answer.delta`として発火させる。

`question-answering-direct-answer-slice.md`が確立した外側の`DirectAnswerer`、blank retry、
typed error、audit / metric契約は維持する。本仕様は、その文書にある内部
`DirectAnswerGenerator.generate() -> str`だけをincremental stream contractで置き換える後続仕様である。

Stream transport、attempt epoch fencing、所有権確認付きSSE、stage / activity / terminalの
producer配線は実装済みである。本sliceではそれらの公開契約を変更せず、Direct answerの
provider chunkから既存`answer.delta { attemptEpoch, generation, text }`までを接続する。

Redis / SSEはライブ表示の補助経路であり、最終回答の正本にはしない。生成した全文は
従来どおりworker内で集約・検証し、Postgresの既存transactionへ渡す。

## Work definition

### Problem

現在の`GeminiDirectAnswerGenerator`は`generate_content()`の完了を待ち、全文を一度に
`DirectAnswerFlow`へ返す。このため、StreamとSSEが存在しても、回答作成中の本文を
`answer.delta`として送るproducerがない。

単純にprovider chunkごとにRedisへ書くと、次の問題が生じる。

- chunk境界をまたぐ`[[1]]`等のcitation markerが一時表示され、最終Direct回答との差が見える。
- 空白・markerだけでblank retryになるgenerationから、取り消し対象の下書きが発生する。
- 1文字または1chunkごとのXADDがStream件数とRedis timeout待ちを増やす。
- cancel済みまたは新epochへ進んだrunでも、旧workerがprovider streamを消費し続ける。
- Redis全断時に0.5秒timeoutを高頻度に繰り返し、300秒のworker実行時間を圧迫する。
- safety / recitationがstream後半で判明したとき、すでに表示したdraftとrunの失敗を
  別のfailure domainとして扱う必要がある。

本sliceでは、最終Direct回答の意味論を変えずに、表示専用の増分変換、配信頻度制御、
停止判定、Redis障害時の遮断を追加する。

### Evidence

2026-07-12時点の実装を根拠とする。

- `backend/app/agent/answering/direct_answer/flow.py`
  - `_CITATION_MARKER_RE = r"\[\[[0-9]+\]\]"`で、生成後に全citation markerを除去する。
  - marker除去後の`.strip()`が空なら`DirectAnswerInvalidError`とする。
  - 最大2回試行し、`DirectAnswerInvalidError`だけをin-request retryする。
- `backend/app/agent/answering/audit.py`
  - `AIProviderError`は`DO_NOT_RETRY_IN_REQUEST`、`DirectAnswerInvalidError`は
    `RETRY_IN_REQUEST`に分類する。
- `backend/app/agent/answering/direct_answer/contract.py`
  - 現在の`DirectAnswerGenerator.generate()`は未検証の全文`str`を返す。
  - 外側の`DirectAnswerer.answer()`は検証済み`DirectAnswerDraft`を返す。
- `backend/app/agent/answering/direct_answer/ai/gemini.py`
  - 現在は`generate_content()`を使い、`SAFETY` / `RECITATION`を
    `AIProviderOutputBlockedError`へ変換する。
- `backend/app/agent/answering/direct_answer/ai/spec.py`
  - Direct answerの`max_output_tokens`は2048である。
- `backend/app/agent/live_updates/stream.py`
  - `answer.delta`の型、正の`generation`、non-empty `text`、0.5秒publish timeout、
    MAXLEN 4096、TTL 900秒は実装済みである。
  - publishは成功確認時にStream ID、失敗・timeout時に`None`を返す。
- `backend/app/agent/live_updates/sse.py`
  - follow intervalは0.5秒である。250msより細かくpublishしても通常は同じreadへまとまる。
- `backend/app/queue/tasks/agent_run.py`
  - worker timeoutは300秒である。
  - DB commit後のcompleted / failed terminal producerは実装済みである。
- `backend/app/agent/runs/repository.py`
  - cancel、complete、再取得はDB statusと整数`attempt_epoch`で競合を解決する。
  - 現在attemptのworkerがまだ有効かを低コストで確認する専用queryはない。
- `backend/uv.lock`
  - `google-genai==2.10.0`をlockしている。
  - async streaming APIは`await client.aio.models.generate_content_stream(...)`でstreamを取得し、
    `async for`で増分responseを読む。`chunk.text`は`None`になり得る。
  - `finish_reason`は生成停止時に現れるが、物理的な最終SSE responseだけに現れる保証はない。

Gemini APIの判断根拠:

- https://googleapis.github.io/python-genai/genai.html#genai.models.AsyncModels.generate_content_stream
- https://github.com/googleapis/python-genai/blob/v2.10.0/google/genai/models.py#L7200-L7296
- https://ai.google.dev/api/generate-content
- https://github.com/googleapis/python-genai/blob/v2.10.0/google/genai/types.py#L455-L481

既存実装は移行元を示す証拠であり、本sliceの正しい契約そのものではない。

### Invariants

1. Postgresのrun、assistant message、sourcesを最終結果の唯一の正本とする。
2. 外側の`DirectAnswerer.answer() -> DirectAnswerDraft`、最大2回、blankだけretry、既存audit / metricの
   意味論を変えない。
3. provider chunkは増分断片として扱い、全文を順序どおりworker内で集約する。
4. 成功generationで表示filterがreporterへ渡した断片の連結は、既存全文処理
   `DirectAnswerDraft(answer=_CITATION_MARKER_RE.sub("", raw_answer))`の`answer`と一致する。
5. Redis publishがすべて成功した場合、同じgenerationの`answer.delta.text`をStream ID順に
   連結した値は最終`DirectAnswerDraft.answer`と一致する。Redis障害時の欠落は許容し、DB最終回答へ
   劣化する。
6. citation marker、先頭・末尾空白だけから可視deltaを作らない。marker / 空白だけのblank attemptは
   deltaを0件とする。
7. `generation`は`DirectAnswerFlow`の`attempt_number` 1または2である。Direct pathは
   `answer.reset`をpublishしない。
8. `answer.delta`は250msの時間窓を主条件、512 Unicode code pointを上限としてcoalesceする。
   最小文字数gateは設けない。
9. timer、サイズ到達、finishの競合でも、buffer内の文字を欠落・重複・逆転させない。
10. delta publisherの失敗・timeout・例外は、provider stream、最終validation、audit、DB保存、
    run状態遷移を失敗させない。
11. delta publishが3回連続で成功確認できなければ、そのattemptのdelta配信だけを停止する。
    stage / activity / terminalと回答生成は停止しない。
12. execution continuationは`(run_id, attempt_epoch)`に束縛する。DBが`running`かつ同じepochのときだけ
    currentとみなす。
13. cancel、terminal遷移、run不在、より新しいepochを検出したworkerは、専用のroutine stopとして
    provider消費と未配信deltaを止め、`mark_failed`、complete、terminal再publishを行わない。
14. continuation probeのDB障害はfail-openし、probe障害だけでrunを失敗させない。
15. DB session / transactionをRedis I/Oまで保持しない。probeは必要な存在確認だけを短命sessionで行う。
16. provider blockを示すchunkのtextは配信しない。block判明前に配信済みのdraftはrollbackせず、
    DB failedとterminalによりclientが破棄する。
17. Stream / SSEのevent vocabulary、`answer.delta`公開shape、attempt epoch、TTL、MAXLEN、timeoutを
    変更しない。
18. payload、回答本文、provider生response、例外本文、user IDを新しいlog / metricへ記録しない。
19. 正常終了、provider error、blank retry、routine stop、breaker openの全経路でtimerとasync iteratorを
    回収し、background taskを残さない。
20. provider側の物理cancelや課金停止は保証しない。ローカル消費とRedis writeをbest-effortで抑止する。

### Non-goals

- Evidence answerのstructured JSON増分復元。
- Direct pathから`answer.reset`を発火すること。
- browser `EventSource`、React draft state、回答下書きUI。
- Stream / SSE event type、HTTP endpoint、BFF、frontend公開型の変更。
- citation link、source card、missing aspectsの途中表示。
- citation regex、Direct answer prompt、回答品質、retry回数の変更。
- Redis Listの`recentEvents`、DB `progress_stage`、既存pollingの削除。
- Redis Streamを最終回答、監査ログ、長期履歴として使うこと。
- terminal producer、DB finalization transaction、attempt fencingの再設計。
- provider serverでの生成停止、token課金停止、exactly-once delta deliveryの保証。
- DB schema / migration、新規dependency、外部API response shapeの変更。
- evidence sliceに備えたJSON parserや共通reset機構の先行実装。

### Done

- Gemini Direct answerがasync streaming APIを使い、既存と同じ最終`DirectAnswerDraft`を返す。
- citation markerとouter whitespaceを増分経路から除外し、成功時の可視本文と最終本文が一致する。
- 250ms / 512文字coalescingで、同一epoch・正しいgenerationの`answer.delta`を発火する。
- blank retryは第1generationのdelta / resetを作らず、第2generationを`generation=2`で配信する。
- cancel / terminal / epoch前進を検出した旧workerが、run状態を上書きせずroutineに終了する。
- Redis全断時は3回のdelta publish失敗で配信を遮断し、最終回答保存とterminal試行を継続する。
- provider block / 途中例外 / terminal metadata欠落を成功と誤認しない。
- unit、DB integration、worker lifecycle、実Redis integration testで本仕様の保証を固定する。
- 親仕様をEvidence answer draft deltasの検討へ進められる。

### Implementation result

- **Streaming / filter**: `GeminiDirectAnswerGenerator`、`DirectAnswerFlow`、
  `DirectAnswerVisibleTextFilter`を接続し、provider metadata、全文集約、citation / outer whitespace、
  blank retry、iterator cleanupをunit testで固定した。
- **Coalescer / breaker**: `AgentRunLiveAnswerDeltaReporter`へ250ms / 512文字coalescingと
  attempt-localな3連続未確認breakerを実装し、timer race、generation lifecycle、PII-free観測を
  決定的unit testで固定した。
- **Continuation / routine stop**: `AgentRunRepository`、`AgentRunExecutionProbe`、composition、workerを
  接続し、同一epoch確認、2秒cache、DB fail-open、cancel / epoch前進時のroutine stopをunit、
  実Postgres、worker lifecycle testで固定した。
- **Redis境界**: 実Redisでdelta producerからreaderまでを検証し、既存Redis List producerと
  Stream producerが相互に壊れないことをintegration testで固定した。
- **回帰検証**: 既存の最終DB回答、audit / metric、stage / activity / terminal、List polling、
  completion競合時の保存境界を維持した。

検証実績:

- `ruff check`: pass
- `ruff format --check`: pass
- unit `pytest -m 'not integration'`: 3409 passed, 854 deselected
- guarded integration `make test-integration`: 835 passed, 19 skipped
- 上記integrationには、実Redis専用3件、worker lifecycle 42件、probe実Postgres 3件を含む。

## Terminology and constants

### generation

同じworker attempt内でDirect answerをproviderへ要求する回数である。

- 値域: 1以上。本sliceでは1または2。
- source of truth: `DirectAnswerFlow.attempt_number`。
- `attemptEpoch`とは別物である。attempt epochはworker取得、generationは同一worker内のblank retryを表す。

### visible fragment

providerのraw chunkからcitation markerと最終結果で除去されるouter whitespaceを除いた、
ユーザーへ表示可能な増分文字列である。raw provider chunkをそのままRedisへ渡さない。

### coalescing window

最初のpending fragmentが入った時点から、次のRedis publishを行うまでの最大待ち時間である。
idle中に常駐する周期loopは作らない。

### execution continuation

acquire済みworkerが、現在もそのrunの同じepochを実行する権利を持つかというcost-control判定である。
DB遷移の正しさやepoch fencingの代替ではない。

### Initial constants

| constant | value | meaning |
|---|---:|---|
| delta flush interval | 0.25秒 | 最初のpending fragmentから独立timerでflushする最大待ち時間 |
| delta max characters | 512 | 1つの`answer.delta.text`に含めるUnicode code point上限 |
| continuation check interval | 2.0秒 | 実DB queryを再実行する最短間隔 |
| consecutive publish failure threshold | 3 | delta circuit breakerをopenする連続未確認回数 |

これらは初期運用値であり、event数、publish latency、completion latencyの実測に基づく後続変更を
許容する。ただし変更時はMAXLEN 4096、0.5秒publisher timeout、2048 output token、SSE follow
interval 0.5秒を同時に再評価する。

## Responsibility model

```text
GeminiDirectAnswerGenerator
  |-- generate_content_stream
  |-- chunkごとのblock / finish metadata検査
  `-- incremental textをyield

DirectAnswerFlow
  |-- raw全文の順序付き集約
  |-- citation / outer whitespace増分filter
  |-- generation 1..2とblank retry
  |-- optional AnswerGenerationContinuationの消費
  `-- optional AnswerDeltaReporterへのvisible fragment通知

worker boundary
  |-- (run_id, attempt_epoch) execution probe
  |-- 250ms / 512文字coalescer
  |-- attempt-local delta circuit breaker
  `-- AgentRunLiveStreamPublisher

worker finalization
  `-- 既存DB transaction commit -> terminal publish
```

agent coreはRedis、run ID、attempt epoch、SSE、HTTPを知らない。workerはrun固有の値を
optional protocol実装へbindし、compositionからDirect flowへ注入する。

## Design

### 1. Internal contracts

`DirectAnswerGenerator`の内部contractは、全文返却からincremental streamへ置き換える。
移行期間に`generate()`と`stream()`の2経路を正本として残さない。

概念contract:

```python
class DirectAnswerGenerator(Protocol):
    def stream(...) -> AsyncIterator[str]: ...

class AnswerDeltaReporter(Protocol):
    async def append(self, *, generation: int, text: str) -> None: ...
    async def finish(self, *, generation: int) -> None: ...
    async def abort(self, *, generation: int) -> None: ...

class AnswerGenerationContinuation(Protocol):
    async def should_continue(self) -> bool: ...
```

- generatorがyieldする値は累積全文ではなく増分textである。
- generatorはprovider metadataをdomain外へ漏らさず、blocked / malformed streamをtyped
  `AIProviderError`へ変換する。
- reporterへ渡すtextはnon-empty visible fragmentだけである。
- `finish()`は正常validation後の残余flush、`abort()`は未配信bufferの破棄とtimer回収を表す。
- reporterはbest-effortであり、routine stop signalには使わない。reporterの失敗はflowから握りつぶす。
- continuationがfalseの場合、flowは専用`AnswerGenerationStopped`を送出する。これは
  `AIProviderError` / `DirectAnswerInvalidError`ではない。
- 外側の`DirectAnswerer` / `QuestionAnsweringAgent` / `AnswerQuestionResult`は変更しない。

`AnswerDeltaReporter`はDirect固有のcitationやGeminiを知らず、`generation`と表示可能なtextだけを扱う。
`AnswerGenerationContinuation`はDB、run ID、epochをinterfaceへ露出しない。

### 2. Gemini async streaming adapter

`GeminiDirectAnswerGenerator`はlock済み`google-genai==2.10.0`のasync APIを次の順で扱う。

1. 既存promptと`GenerateContentConfig`を変更せず構築する。
2. `await client.aio.models.generate_content_stream(...)`でstreamを取得する。
3. `async for chunk in stream`で全responseを順に処理する。
4. 各chunkでtextより先に`prompt_feedback`、candidate、`finish_reason`を検査する。
5. 有効な`chunk.text`だけを増分fragmentとしてyieldする。`None` / empty textはyieldしない。
6. 正常・異常を問わずSDK iteratorをbest-effortでcloseする。

API call開始時とiteration途中のexceptionは、現在と同じ`translate_gemini_error()`を通す。

#### Block and terminal rules

- `prompt_feedback`が入力blockを示す場合は、既存`AIProviderInputRejectedError`と
  `GeminiContentRejectionReason.INPUT_BLOCKED`へ変換する。
- 任意chunkの`finish_reason`が`SAFETY` / `RECITATION`なら、既存
  `AIProviderOutputBlockedError`へ変換する。同じchunkにtextがあってもyieldしない。
- block判明前のchunkはproviderによるrollback保証がない。すでにpublish済みのdraftが一時的に
  見えることを受容し、workerのfailed遷移とterminalでclientが破棄する。
- stream全体で1つ以上の`finish_reason`を観測してから正常終了とする。
- `STOP`、`MAX_TOKENS`等、現在のblocked setに含まれないterminal reasonは従来互換で受理する。
  本sliceでfinish policyを拡張しない。
- exceptionなしのEOFでもterminal reasonを一度も観測しなければ、途中切断として
  `AIProviderNetworkError`へ変換する。PII-freeな診断理由として
  `GeminiStateReason.STREAM_TRUNCATED`を追加する。

### 3. DirectAnswerFlow streaming and retry

各generationは次の順序で処理する。

1. provider request開始前にcontinuationを確認する。
2. raw chunkを順序どおり全文bufferへ追加する。
3. 各chunk処理前にcontinuationを呼ぶ。2秒cacheにより通常はDB queryにならない。
4. raw chunkを増分表示filterへ渡し、得られたvisible fragmentをreporterへ渡す。
5. providerがterminal metadata付きで終了したらfilterをfinalizeする。
6. raw全文へ既存`_CITATION_MARKER_RE.sub("", answer)`と`DirectAnswerDraft` validationを適用する。
7. validation成功時だけfilterの最終fragmentをreportし、reporterを`finish()`する。
8. blankならreporterを`abort()`し、既存分類に従ってgeneration 2へretryする。

reporterの`append()` / `finish()` / `abort()`はすべてbest-effortで呼び、失敗を元の生成結果へ
伝播させない。異常終了では、発生した元例外を保持したまま未配信bufferをabortする。

provider error後にin-request retryしない既存規則を維持する。したがって、可視deltaを出した
generationの後に別generationへ切り替わる経路はDirect pathにはない。`answer.reset`は不要である。

### 4. Incremental citation and whitespace filter

表示filterの正本は新しい独自syntaxではなく、既存最終変換である。

成功時には、任意のchunk分割に対して次を満たす。

```text
concat(visible_fragments)
  == DirectAnswerDraft(
       answer=_CITATION_MARKER_RE.sub("", concat(raw_chunks))
     ).answer
```

#### Citation rules

- `[[`、1文字以上のASCII数字、`]]`から成るmarkerを除去する。
- markerがchunkをまたいでも同じ結果にする。
- 複数marker、隣接marker、marker prefixが重なる入力でも全文regexと同じ結果にする。
- `[x]`、`[[x]]`、`[[12]`等のmalformed candidateはliteralとして残す。
- EOF時の未完成candidateは、全文regexでも除去されないためliteralとして残す。
- 数字の桁数に16文字等の任意上限を置かない。保留量はprovider output上限で有界である。
- filterはcitationを解決・link化せず、該当markerを表示経路から除くだけである。

#### Whitespace rules

- 最初の非空白文字より前のwhitespaceは配信しない。
- 非空白文字の後に現れたwhitespaceは、後続の非空白文字が来るまで保留する。
- 後続に非空白文字が来た場合は内部whitespaceとして順序どおり配信する。
- EOF時に残るtrailing whitespaceは配信しない。
- Python / Pydanticの既存strip semanticsと同じUnicode whitespaceを扱う。

これにより、`"[[1]] \n"`等のmarker / whitespaceだけのgenerationはvisible fragmentを1文字も
作らず、blank retry時に古いdraftを取り消す必要がない。

### 5. Delta coalescer

worker境界の`AnswerDeltaReporter`実装は、filter済みfragmentをgeneration単位でbufferし、
`AgentRunLiveStreamAnswerDeltaEvent`へ投影する。

#### Flush rules

- empty bufferへ最初のfragmentが入った時点で、独立した250ms timerを1つ開始する。
- timerは次provider chunkを待たずに発火する。
- bufferが512 code pointへ到達したら、timerを待たず512文字を即時flushする。
- 513文字以上の入力は512文字以下の複数deltaへ順序どおり分割する。
- サイズflush後に残余があれば、その残余用に新しい250ms windowを開始する。
- `finish(generation)`はtimerをcancelし、残余を1回だけ即時flushする。
- `abort(generation)`はtimerをcancelし、残余をpublishせず破棄する。
- empty text、empty buffer、finish済み / abort済みgenerationからeventを作らない。

250msは文字が少なくてもflushする最大待ち時間であり、最小文字数条件ではない。512はbyte数・token数・
grapheme数ではなくPython `str`のUnicode code point数で数える。分割後に連結すれば元textへ戻る。

timer、append、size flush、finish、abortは同じgeneration lockで直列化する。timer taskのexceptionを
未回収にせず、全exitでcancel / awaitする。

### 6. Delta publish circuit breaker

Redis障害時の高頻度timeout増幅を防ぐため、delta coalescerにattempt-localなcircuit breakerを置く。
共通`AgentRunLiveStreamPublisher`、stage / activity adapter、terminal producerへは入れない。

#### State rules

- publisherがnon-`None`のStream IDを返した場合を成功確認とし、連続失敗数を0へ戻す。
- publisherが`None`を返す、またはunexpected exceptionを送出した場合を1回の未確認として数える。
- 3回連続で未確認になったらopenする。
- open時にpending bufferを破棄し、timerをcancelする。
- open後は同じattemptでdelta publisherを再び呼ばず、fragmentもbufferしない。
- half-open、同一attempt内の自動復帰、失敗deltaの再送は行わない。
- 新worker attemptは新しいreporter instanceを持ち、closed状態から開始する。

publish timeout時はXADDが成功していた可能性があるため、「Redisへ未到達」とは断定しない。
breakerは配信の正確性ではなく、回答生成のwall clockをRedis障害から隔離する機構である。

open後も次を継続する。

- provider streamの消費と全文集約
- Direct answer validation / audit / metric
- 最終DB transaction
- stage / activity producer
- DB commit後のterminal publish試行

open時のlogはattemptにつき1回とし、固定event名、run ID、attempt epoch、generationだけを許可する。
metricは固定reasonだけをlabelとし、run ID / user ID / epoch / generationをlabelへ入れない。

### 7. Execution continuation probe

continuation probeはworker境界で`run_id`、acquire済み`attempt_epoch`、session factoryをbindする。
agent coreへ渡すのは引数なしの`should_continue() -> bool`だけである。

実DB queryは主キーを使う単一の存在確認とする。

```sql
SELECT 1
FROM agent_runs
WHERE id = :run_id
  AND status = 'running'
  AND attempt_epoch = :attempt_epoch
LIMIT 1
```

- 必要なrowや全columnをloadしない。
- repository内でcommitしない。
- 新しいindexを追加しない。主キー`agent_runs.id`で1rowへ絞る。
- rowありだけをtrueとする。
- missing、queued、completed、failed、cancelled相当、epoch不一致をfalseとする。

#### Interval cache

- provider request開始前の初回`should_continue()`は実DB queryを行う。
- 直前の実queryから2秒未満の呼び出しはcached trueを返す。
- 2秒到達後の次の呼び出しがDBを再確認する。
- falseはそのworker instanceでterminalとし、以後trueへ戻さない。
- DB exceptionはtrueとしてfail-openし、その結果も2秒間cacheしてDB hammerを防ぐ。
- 時間判定はmonotonic clockを注入可能にし、wall clock変更の影響を受けない。

この機構はprovider chunkを受け取る機会に確認する。providerから長時間何も届かない間も2秒以内に
停止するwall-clock保証は行わない。次のchunkまたは正常終了処理で再確認し、ローカル消費を止める。

DB exceptionはpayloadや例外文字列を含まない固定warning / counterで可視化する。sessionはquery完了後に
閉じ、同じsessionやtransactionをRedis publishへ持ち越さない。

### 8. Routine stop and worker wiring

compositionはDirect flowへ、workerが作った同一attemptのdelta reporterとcontinuationをinjectする。
Evidence pathは本sliceのreporterを消費しない。

continuationがfalseの場合:

1. flowは現在generationを`abort()`し、pending deltaを破棄する。
2. flowはDirect generatorのasync iteratorをbest-effortでcloseする。
3. flowは専用`AnswerGenerationStopped`をraiseする。
4. workerはgeneration failure / unexpected errorより前の専用exceptで受ける。
5. workerはinfo logだけを残してreturnする。
6. `complete_run()`、`mark_failed()`、terminal publishを行わない。

cancel API、別worker、stale sweep等、DB遷移を成立させた側がrun状態の正本を所有する。routine stopした
workerが同じ状態を再作成しない。新attempt後の旧deltaがすでにRedisへ到達しても、既存epoch fencingが
consumerから除外する。

### 9. Finalization and failure ordering

#### Success

1. provider streamがterminal metadata付きで終了する。
2. full answer validationが成功する。
3. reporterが残余deltaをflushして終了する。
4. `DirectAnswerFlow`が`DirectAnswerDraft`を返す。
5. workerが既存`complete_run()` transactionをcommitする。
6. commit成功後に既存terminal producerが`completed`をpublishする。

delta publish失敗やbreaker openでも3以降を失敗扱いにしない。terminalがdeltaを追い越して見える可能性は
補助経路の障害として許容し、clientはDB最終回答を再取得する。

#### Provider / validation failure

1. pending deltaをabortする。
2. すでにpublish済みのdeltaはStreamから削除しない。
3. 既存failure分類、audit、worker`mark_failed()`を実行する。
4. failed commit後に既存terminal producerが`failed`をpublishする。
5. clientはterminalまたはpollingでdraftを破棄する。

blank generationはvisible deltaが0件なので、generation 2開始前に`answer.reset`を送らない。

### 10. Failure visibility and data exposure

新しい観測信号は次の2種類に限定する。

- delta circuit breaker open count
- execution continuation probe unavailable count

metric labelは固定のfailure分類だけを持つ。logに含めてよい値は固定event名、run ID、attempt epoch、
generation、error class名等のPII-freeな診断属性だけである。

次をlog、metric、trace attribute、exception messageへ含めない。

- delta text、full answer、citation candidate
- provider chunk / response / prompt
- question、conversation context、previous answer
- Redis envelope / payload
- SDK / Redis / DB exceptionの自由文字列
- user ID

## Required file changes

実装時の想定変更範囲を次に固定する。実態確認で責務が異なる場合は、scopeを広げる前に仕様を更新する。

```text
backend/app/agent/contract.py
backend/app/agent/answering/direct_answer/contract.py
  # optional delta reporter / continuation / routine stopとstream generator contract

backend/app/agent/answering/direct_answer/stream_filter.py (new)
  # citation marker + outer whitespace incremental filter

backend/app/agent/answering/direct_answer/flow.py
  # raw全文集約、generation、filter、reporter lifecycle、continuation消費

backend/app/agent/answering/direct_answer/ai/gemini.py
backend/app/analysis/gemini_error_translator.py
  # generate_content_stream、全chunk metadata、stream truncated分類

backend/app/agent/live_updates/answer_delta.py (new)
backend/app/agent/live_updates/metrics.py
  # 250ms / 512文字coalescer、attempt-local breaker、低cardinality観測

backend/app/agent/runs/repository.py
backend/app/agent/runs/execution_probe.py (new)
  # current run/epoch existence queryと2秒fail-open cache

backend/app/agent/composition.py
backend/app/queue/tasks/agent_run.py
  # worker-bound adapter / probe注入とroutine stop分岐

backend/tests/agent/answering/direct_answer/test_stream_filter.py (new)
backend/tests/agent/answering/direct_answer/test_flow.py
backend/tests/agent/answering/direct_answer/ai/test_gemini.py
backend/tests/agent/live_updates/test_answer_delta.py (new)
backend/tests/agent/runs/test_execution_probe.py (new)
backend/tests/agent/test_agent_run_task.py
backend/tests/agent/live_updates/test_answer_delta_integration.py (new or existing integration)

backend/specs/agent-answer-streaming-sse.md
  # Direct path契約とslice進行状況の同期
```

DB schema、SQLAlchemy model、Alembic migration、FastAPI / Pydantic response schema、frontend generated typeを
変更しない。必要になった場合は本sliceを停止し、Ask First対象として再判断する。

## Tests

時間依存testは実sleepを使わず、注入したclock / timerで決定的に検証する。既存transport / SSE testが
所有するepoch fencing、frame serialize、TTL / MAXLEN全般を重複テストせず、新producerから既存transportへ
正しいeventが通る境界を追加する。

### Unit: incremental filter

1. `[[1]]`を全chunk境界で分割しても除去結果が同じである。
2. 複数・隣接・prefixが重なるmarkerが全文regexと一致する。
3. 16桁を超える数字markerも除去し、任意長capを持たない。
4. malformed markerとEOF時未完成prefixをliteralとして残す。
5. 先頭・末尾whitespaceを除き、内部whitespace、改行、Unicodeを保持する。
6. whitespaceがchunkをまたいでも最終strip結果と一致する。
7. marker / whitespaceだけならvisible fragmentを0件とする。
8. representative inputの全chunk分割について、visible fragment連結が既存最終変換と一致する。

### Unit: Gemini streaming adapter

1. `generate_content_stream()`を使用し、incremental `chunk.text`を順序どおりyieldする。
2. `None` / empty textはyieldせず、後続metadataを検査する。
3. API call開始時とiteration途中のexceptionを既存translatorで変換する。
4. 全chunkで`SAFETY` / `RECITATION`を検査し、同じchunkのtextをyieldしない。
5. block前のchunkはrollbackしないことを固定する。
6. prompt feedback blockを`AIProviderInputRejectedError(INPUT_BLOCKED)`へ変換する。
7. `STOP`と`MAX_TOKENS`をterminalとして受理する。
8. terminal reasonなしEOFを`AIProviderNetworkError(STREAM_TRUNCATED)`とする。
9. early stopとiteration failureでSDK iteratorをcloseする。
10. prompt、model、temperature、max output tokens、rate limit policyを変更しない。

### Unit: DirectAnswerFlow

1. incremental chunkをraw全文へ集約し、既存最終answer、audit、metricを維持する。
2. 成功generationのreported fragment連結がfinal draftと一致する。
3. marker / whitespaceだけのgeneration 1はdelta 0件でgeneration 2へ進み、resetを呼ばない。
4. generation 2のdeltaへ`generation=2`を渡す。
5. 2回ともblankなら両generationでdelta / reset 0件のまま既存invalid errorをraiseする。
6. `AIProviderError`ではretryせず、pending reporterをabortする。
7. reporterのappend / finish / abort失敗でも、生成結果または元例外を変えない。
8. continuation falseではiteratorとreporterをabortし、専用routine stopをraiseする。

### Unit: coalescer and circuit breaker

1. 最初のfragmentから250msで、次chunkを待たずflushする。
2. window内のfragmentを順序どおり1つへまとめる。
3. 512文字を1件、513文字を512 + 1、1025文字を512 + 512 + 1へ分ける。
4. normal finishは残余を1回だけflushし、emptyならpublishしない。
5. abortはpendingをpublishせず、timerをcancel / awaitする。
6. timer、size flush、finishの同時raceで重複・欠落・順序逆転がない。
7. generationをdelta eventへそのまま投影し、empty text eventを作らない。
8. publisher `None`とexceptionを未確認として数え、3回連続でopenする。
9. `fail, fail, success, fail, fail`ではsuccessがcountをresetし、openしない。
10. open後は同じattemptでpublisherを呼ばず、buffer / timerを作らない。
11. 新reporter instanceはclosed breakerから開始する。
12. breaker openを1回だけ観測し、本文・例外本文・user IDを記録しない。
13. terminal publisherはdelta breakerの対象外である。

### Unit and DB integration: execution continuation

1. DBがrunningかつ同じepochの場合だけtrueを返す。
2. run不在、queued、completed、failed、epoch不一致でfalseを返す。
3. 初回はqueryし、2秒未満はcache、2秒到達後の次回に再queryする。
4. DB exceptionはtrueへfail-openし、2秒間cacheしてquery stormを防ぐ。
5. falseはterminal cacheとなり、同じworkerで再queryしてtrueへ戻さない。
6. queryは必要な存在だけを読み、repository内commitやrow全体loadを行わない。
7. sessionがquery後に閉じ、Redis publisherへ渡らない。
8. actual Postgresでcancel遷移後にfalseとなる。
9. actual Postgresで再取得によりepochが進んだ後、旧epoch probeがfalseとなる。

### Worker lifecycle

1. acquire成功時だけ同じrun ID / epochのreporterとprobeを作り、Direct flowへ注入する。
2. Direct成功時はdelta finish、final DB commit、completed terminalの順で処理する。
3. blank retryではgeneration 1のdelta / resetがなく、generation 2のdeltaだけを送る。
4. delta publish全失敗時は3回でbreakerがopenしても回答全文をDBへ保存し、terminalを試行する。
5. cancel検出のroutine stopではcomplete / mark_failed / terminal再publishを行わない。
6. epoch前進を検出した旧workerも同じroutine stopへ入る。
7. probe DB障害はfail-openして正常生成を継続する。
8. 一部delta後のprovider block / network failureはfailed commit後にterminalを送り、assistant messageを
   保存せず、pending deltaをflushしない。
9. `complete_run()`競合負けでは、deltaが存在してもloserがassistant messageやterminalを作らない。
10. 全exit経路でtimerとprovider iteratorを回収する。

### Real Redis integration

1. fake streaming generatorからfilter、coalescer、実Stream publisher、readerを通し、
   `attempt.started`後に同じepoch / generationのdeltaをStream ID昇順で読める。
2. Redis正常時のdelta連結がfinal `DirectAnswerDraft.answer`と一致する。
3. 512文字分割とgeneration 2が既存envelopeから正しくdecodeされる。
4. delta failure経路が既存stage / activity / terminal、List `recentEvents`へ影響しない。
5. testは所有するUUID keyだけを削除し、共有Redisへ`FLUSHDB`しない。

### Regression

1. Direct answerのfinal DB text、status、sources空配列、missing aspects空配列が変わらない。
2. blank retry回数、previous error、audit event、outcome metricが変わらない。
3. Evidence answerのgenerator / flow / JSON contractを変更していない。
4. `answer.delta`のRedis / SSE公開shapeとcamelCase境界が変わらない。
5. stage / activity / terminal producer、DB progress、List pollingが回帰しない。
6. log / metric / exceptionへ回答本文、provider payload、質問、user IDが漏れない。

## Implementation order

1. incremental filterの同値性testを先に追加し、citation / whitespace変換を独立実装する。
2. Gemini stream adapter testを追加し、全chunk metadataとterminal判定を実装する。
3. internal reporter / continuation contractとDirect flow testを追加し、全文集約・retryをstreamへ移す。
4. timer注入可能なcoalescerとdelta circuit breakerをtest-firstで実装する。
5. current run/epoch existence queryと2秒cache probeをDB test付きで実装する。
6. composition / workerへ同じattemptのreporterとprobeを接続し、routine stop分岐を追加する。
7. 実Redisでproducerからreaderまでの境界を検証する。
8. backendの関連test、lint、type check、format checkを実行する。
9. 親仕様のslice進行状況を更新し、Evidence answer draft deltasへ進む。

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| block判明前のdraftが見える | failed回答の一部が一時表示される | 同一blocked chunkは送らず、terminal / pollingでdraftを破棄。streaming固有リスクとして受容 |
| citation candidateを早く確定する | markerの断片や最終回答との差が見える | 全文regexと等価な増分filter。桁数capを置かない |
| 250ms timerのrace | delta重複、順序逆転、task leak | generation lockと決定的timer test、finish / abortでcancel + await |
| Redis全断がworker時間を消費 | provider成功前にTaskiq timeoutへ接近 | 3連続未確認でdeltaだけをattempt中停止 |
| breaker open後にRedisが復旧 | 同attemptのlive draftが再開しない | DB最終回答へ劣化。half-openの複雑さを初期sliceへ入れない |
| cancel検出が最大2秒程度遅れる | provider / Redisを短時間余分に消費 | chunk機会ごとのprobe + 2秒cache。正しさはDB遷移とepoch fencingが担保 |
| providerがchunkを長時間返さない | probeが2秒ごとに走らない | hard cancelは非目標。次chunk / iterator終了時に検出 |
| probe DB障害 | cancel抑止が一時的に効かない | fail-openし回答生成を守る。固定metricで可視化 |
| terminal metadataなしEOF | 不完全回答を成功保存する | stream truncatedをtyped provider errorへ変換 |
| early iterator closeでprovider課金が止まらない | 外部コストが完全には止まらない | ローカル消費停止だけを保証し、provider server cancelは非目標 |

## Acceptance summary

本sliceの中心は「providerが返した文字を早く出す」ことではなく、次の二層を同時に守ることである。

- **正常時**: Direct answerの最終変換と同じ本文だけを、250ms / 512文字単位で順序どおり表示する。
- **障害時**: live draftの欠落・短命化を許容し、回答生成とPostgres最終結果を守る。

Direct pathではblank retryから可視draftが発生しないためresetを作らない。cancel / stale workerの抑止と
Redis circuit breakerは正しさを再実装する機構ではなく、すでにDB / epoch fencingで確立した正しさの上で
無駄なprovider消費・Redis待ちを抑えるcost-controlである。
