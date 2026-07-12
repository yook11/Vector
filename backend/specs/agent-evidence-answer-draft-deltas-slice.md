# Agent Evidence answer draft delta streaming slice 仕様

Status: Implemented — 2026-07-12

親仕様: `agent-answer-streaming-sse.md`

前提slice:

- `question-answering-evidence-synthesis-slice.md`
- `question-answering-inline-citation-slice.md`
- `agent-live-stream-transport-slice.md`
- `agent-attempt-epoch-fencing-token-slice.md`
- `agent-sse-backend-bff-slice.md`
- `agent-live-event-producer-wiring-slice.md`
- `agent-direct-answer-deltas-slice.md`

後続slice:

- Research UI draft rendering
- Operational verification

## Positioning

本sliceは、structured JSONで生成するEvidence answerから、ユーザーへ表示可能な`answer`文字列だけを
増分復元し、既存Redis Streamの`answer.delta`として発火させる。

JSON構文、field名、`sufficiency`、`cited_refs`、`missing_aspects`、未完成escapeをfrontendへ
公開しない。生成途中の本文は下書きであり、最終回答の正本にはしない。AI出力全体の受信後に既存の
schema・citation・evidence検証を完了し、成功または既存fallbackをPostgresへ永続化する。

Evidence pathでは、表示済み本文を持つgenerationが最終検証で失敗し、同一worker内でretryされ得る。
retry時は古い下書きを破棄してから次generationを開始する。Redis / SSEの配信完了やfrontendの描画完了を
DB永続化の条件にはしない。

## Work definition

### Problem

現在の`GeminiEvidenceAnswerDraftGenerator`は`generate_content()`の完了を待ち、response text全体を
`json.loads()`して`RawEvidenceAnswerDraft`を返す。そのため、Evidence answerの生成中に本文を表示する
producerがない。

単純にprovider chunkをそのまま送ると、次の問題が生じる。

- JSONの`{"answer": ...}`、他field、delimiter、escape途中がユーザーへ見える。
- chunk境界で`\\uXXXX`、surrogate pair、escaped quote、backslash等を誤って表示する。
- field順序が変わったとき、`answer`以外の文字列を本文として扱う。
- 最終JSON、schema、citation、evidence検証に失敗してretryしたとき、旧本文と新本文が混ざる。
- `answer.reset`だけを破棄境界にすると、reset publish喪失時にgenerationが混ざる。
- frontend接続や描画ackを永続化条件にすると、ブラウザ切断がrun完了を妨げる。
- provider / Redis障害が回答生成や既存fallback、DB保存へ波及する。

本sliceでは、既存Evidence synthesisの最終意味論を変えず、JSONから表示可能本文だけを抽出する増分境界と、
retry時のgeneration切替を追加する。

### Evidence

以下は2026-07-12の実装開始時点に確認した移行元の証拠である。

- `backend/app/agent/answering/evidence_answer/ai/gemini.py`
  - `generate_content()`完了後に`response.text`を`json.loads()`する。
  - JSON objectでない応答をtyped `EvidenceAnswerDraftGenerationInvalidError`へ変換する。
  - `SAFETY` / `RECITATION`を`AIProviderOutputBlockedError`へ変換する。
- `backend/app/agent/answering/evidence_answer/ai/spec.py`
  - `response_mime_type=application/json`とGemini response schemaを使う。
  - output token上限は2048である。
- `backend/app/agent/answering/evidence_answer/ai/schema_tool.py`
  - required fieldは`sufficiency`、`answer`、`cited_refs`、`missing_aspects`である。
  - schema上のproperty順をconsumer契約として固定していない。
- `backend/app/agent/answering/evidence_answer/flow.py`
  - response envelope、schema、citation、evidence不整合は最大1回in-request retryする。
  - provider errorはretryせず、既存のinsufficient fallbackへ収束する。
  - retry後も検証できない場合も同じfallbackへ収束する。
- `backend/app/agent/answering/evidence_answer/validation.py`
  - 本文中の`[[N]]`から`cited_refs`を再計算する。
  - answeredでmarkerがない場合や、存在しないevidence refを参照した場合はretry対象となる。
  - final `EvidenceAnswerDraft.answer`は`NonBlankText`としてouter whitespaceをstripする。
- `backend/app/agent/answering/orchestration.py`
  - strict draftからsources、missing aspects、最終`AnswerQuestionResult`を組み立てる。
- `backend/app/agent/live_updates/stream.py`
  - `answer.delta`と`answer.reset`、正のgeneration、attempt epoch fencingは実装済みである。
- `backend/app/agent/live_updates/answer_delta.py`
  - 250ms / 512文字coalescing、generation lifecycle、attempt-local circuit breakerは実装済みである。
- `backend/app/agent/runs/execution_probe.py`
  - cancel / terminal / epoch前進を2秒cache付きで検出する継続判定は実装済みである。
- `backend/app/queue/tasks/agent_run.py`
  - 最終DB commit後にterminalをpublishする。

既存実装は移行元を示す証拠であり、本sliceの正しい契約そのものではない。

### Invariants

1. Postgresのrun、assistant message、sourcesを最終結果の唯一の正本とする。
2. frontendへ送る本文は、structured JSONの`answer` stringをJSON規則どおりdecodeした文字だけとする。
3. JSON delimiter、key、`sufficiency`、`cited_refs`、`missing_aspects`、未完成escapeを
   `answer.delta`へ入れない。
4. raw provider chunk、raw JSON、provider responseをRedis、DB draft、log、metricへ保存しない。
5. AI出力全体を受信し、JSON object化、`RawEvidenceAnswerDraft`化、既存finalization、citation / evidence検証を
   完了してから、既存worker transactionへ最終結果を渡す。
6. frontendが全deltaを受信・描画したことを確認せずDBへ永続化する。SSE client ackを追加しない。
7. retry時はgeneration 1をabortし、`answer.reset { generation: 2 }`をbest-effortでpublishしてから
   generation 2のdeltaを開始する。
8. consumerは、現在より大きいgenerationの`answer.reset`または`answer.delta`を受信した時点で、古い下書きを
   破棄してから新eventを適用する。正しさをreset entryの到達だけに依存させない。
9. 同じgenerationの重複resetは下書きを再破棄する境界にしない。
10. generationは表示revisionであり、表示内容を置き換えるたびに1増やす。初回は1、retryは2、
    retry後のfallbackは3、retryしないfallbackは2とする。
11. fallbackへ進む場合も、現在generationをabortし、generationを1増やしたreset後に既存の安全な
    fallback本文をdeltaとして配信する。
12. Evidence下書きでは完全な`[[N]]` citation markerを表示しない。malformed markerは通常本文として残す。
13. Redis publishが成功した正常generationでは、Stream ID順のdelta連結が次の具体式と一致する。

    ```python
    concat(visible_deltas) == _CITATION_MARKER_RE.sub(
        "",
        EvidenceAnswerDraft.answer,
    ).strip()
    ```

    citation marker除去後にouter whitespaceをstripし、演算順序を逆にしない。
14. object直下に同じtop-level keyが重複したJSONは、live配信状態に依存しないEOF後の最終parserを正本として
    不正generationへrejectし、既存retry / fallbackへ進める。`answer`以外のmetadataもlast-winsへ依存しない。
15. invalid generationで配信済みの本文を、retry / fallback後のgenerationへ混ぜない。
16. top-level重複keyのreject追加を除き、JSON / schema / citation検証、最大2attempt、previous error、
    fallback、audit / metricの既存意味論を変えない。
17. delta / reset publish失敗は、provider stream、validation、retry、fallback、DB保存、terminal試行を
    失敗させない。
18. delta / resetは同じattempt-local breaker counterを共有し、合計3回連続未確認でopenする。open後は
    delta / resetをともに抑止し、terminal producerは抑止しない。
19. Direct sliceの250ms / 512文字coalescing、delta circuit breaker、execution continuation probe、
    routine stopを再利用し、Evidence専用の並行配信基盤を作らない。
20. cancel、terminal、epoch前進を検出したworkerは、pending本文とprovider iteratorを止め、
    `complete_run()`、`mark_failed()`、terminal再publishを行わない。
21. provider blockを示すchunkのtextは配信しない。block判明前に配信済みの下書きは、retry / fallback /
    terminal時の破棄規則で収束させる。
22. normal、retry、fallback、provider error、parse error、routine stop、breaker openの全経路で、
    timerとasync iteratorを回収する。
23. `terminal(completed)`は最終assistant messageとrun状態のDB commit後にだけpublishする。terminal受信時には
    frontendが取得すべき確定結果がDBに存在する。
24. Stream / SSE event vocabulary、attemptEpoch、TTL、MAXLEN、timeout、HTTP endpoint、BFF認可を変更しない。

### Non-goals

- JSON全体、schema、LLMの思考過程、tool call、promptをfrontendへ公開すること。
- frontendのReact draft state、citation link、source card、missing aspects UIを実装すること。
- 回答下書きをPostgresやRedisの長期履歴へ保存すること。
- frontend ack、WebSocket、consumer group、exactly-once deliveryを追加すること。
- Evidence answer prompt、response schema、citation規則、retry回数、fallback本文を変更すること。
- Direct answer streamingを再設計すること。
- terminal producer、DB finalization transaction、attempt fencing、SSE endpointを変更すること。
- provider server側の生成停止や課金停止を保証すること。
- DB schema / migration、新規dependency、外部API response shapeを変更すること。

### Done

- Evidence Gemini adapterがasync streaming APIを使い、raw JSONをworker内で全文集約できる。
- 増分JSON extractorが`answer` stringだけをdecodeし、JSON形式や他fieldを一度もdeltaへ出さない。
- escape、Unicode、surrogate pair、field順序、chunk分割に依存せず、正常時のdelta連結がcitation markerを
  除いた最終表示本文と一致する。
- object直下の全top-level重複keyを不正としてretry / fallbackへ収束させる。
- retry前に旧generationをabortし、reset境界の後にgeneration 2だけを継続表示できる。
- fallback前にも旧generationをabortし、次generationのreset後に安全なfallback本文を表示できる。
- reset entryを取りこぼしても、大きいgenerationのdeltaで旧下書きを破棄できる契約が固定される。
- AI出力と既存検証の完了後にDBへ永続化し、frontendの受信完了を待たない。
- retry / fallback / provider failure / cancel / epoch前進 / Redis障害で既存run状態と最終DB結果を壊さない。
- unit、worker lifecycle、実Redis integration testで本仕様の保証を固定する。
- 親仕様をResearch UI draft renderingへ進められる。

## Implementation evidence

- Gemini Evidence adapterをraw async streamへ移行し、shared `AnswerVisibleTextFilter`、root
  `answer`のincremental extractor、EOFでtop-level重複keyを拒否するparserを接続した。
- `EvidenceAnswerFlow`はgeneration 1 / 2のretryとreset、generation 2 / 3のfallback置換を扱い、
  Directと共有するcontinuation契約でroutine stopへ収束する。
- reset / deltaは同じattempt-local breakerを共有する。composition / workerは同一のreporterとprobeを
  Direct / Evidenceへ注入し、DB commit後にterminalをpublishする既存境界を維持した。
- 実Redisでは通常retryのreset順序と、reset喪失時にも大きいgenerationのdeltaがimplicit resetになる
  event列を検証した。
- backendのevent契約は完成した。frontendのResearch UI consumerとdraft state machineは後続sliceが所有する。
- Verification: Ruff check pass / format check pass / unit `3473 passed, 857 deselected` /
  integration `838 passed, 19 skipped` / focused Redis `5 passed`。

## Responsibility model

```text
GeminiEvidenceAnswerDraftGenerator
  |-- generate_content_stream
  |-- raw JSON fragmentsをyield
  |-- chunkごとのblock / finish metadata検査
  `-- 完全JSONの意味解釈は行わない

EvidenceAnswerFlow
  |-- raw JSON全文の順序付き集約
  |-- incremental answer string extractor
  |-- generation 1..3の表示revisionと既存retry / fallback
  |-- existing final json.loads / RawEvidenceAnswerDraft / validation
  |-- optional continuation
  `-- optional delta / reset reporter

worker境界
  |-- same attemptのStream publisher
  |-- 250ms / 512文字coalescer + breaker
  |-- run / epoch continuation probe
  `-- DB commit後のterminal producer

frontend（後続slice）
  |-- attemptEpoch / generationでdraft境界を判定
  `-- terminal / polling後にDB final answerへ置換
```

## Draft design

### 1. Provider stream and final parse

Evidence generatorは`generate_content_stream()`からraw text fragmentを順序どおりyieldする。flowは全文を
memory内に集約し、stream終端後にpairsを保持できる最終JSON parser、object確認、
`RawEvidenceAnswerDraft.model_validate()`、`finalize_evidence_answer_draft()`を行う。

増分extractorは表示専用であり、最終parseやvalidationの代替にしない。途中まで正しく見えるJSONでも、
終端後の全文が不正ならgenerationは失敗としてretry / fallbackへ進む。

top-level重複key検出の正本はEOF後の最終parserとする。`json.loads(..., object_pairs_hook=...)`等でobjectの
pair列を保持し、root objectだけのkey一意性を検査してから通常objectへ変換する。extractorが同じ重複を
途中検知して早期停止することは許可するが最適化に限定し、delta reporter不在・breaker open・live配信停止でも
最終parserが必ず同じJSONをrejectする。

top-level重複keyは`EvidenceAnswerDraftGenerationInvalidError`の新しい固定defect code
`evidence_answer_response_duplicate_top_level_key`へ分類する。既存failure classifierにより
`RETRY_IN_REQUEST`となり、初回はretry、2回目はfallbackへ進む。nested objectの重複keyは本sliceの
reject対象にしない。

#### Block and terminal rules

- 各chunkでtextより先にprompt feedback、candidate、`finish_reason`を検査する。
- `SAFETY` / `RECITATION`を含むchunkのtextはextractorへ渡さない。
- `STOP` / `MAX_TOKENS`等、blocked setに含まれないfinish reasonはterminalとして受理する。
- `MAX_TOKENS`でもJSONが完全なら通常の最終parse / validationへ進む。JSONが途中ならgeneration invalidとして
  retry対象になる。
- stream全体でterminal reasonを1件も観測せずEOFした場合は
  `AIProviderNetworkError(reason=STREAM_TRUNCATED)`とし、既存provider error分類によりretryせずfallbackへ進む。
- SDK iteratorの開始・iteration・close exceptionは既存translator / best-effort cleanup規則に従う。

### 2. Incremental answer string extractor

extractorはJSON tokenの文脈を追跡し、object直下の`answer` keyに対応するstring valueだけをdecodeして返す。

最低限、次を扱う。

- 任意のchunk分割、1文字chunk、空chunk
- property順序と、`answer`前後の他field
- whitespace、改行、escaped quote、backslash、slash
- `\\b`、`\\f`、`\\n`、`\\r`、`\\t`
- chunkをまたぐ`\\uXXXX`
- UTF-16 surrogate pairと通常のUnicode文字
- string内の`{`、`}`、`[`、`]`、`,`、`:`
- EOF時の未完成string / escape / JSON

`EvidenceAnswerDraft.answer`の`NonBlankText`と一致させるため、answer stringのouter whitespaceは増分経路でも
除去する。完全な`[[N]]` citation markerは下書きから除去し、citation link / source cardはDB確定後のUIが
所有する。`[1]`、`[[x]]`、`[[12]`等のmalformed markerは通常本文として残す。

object直下で`answer` keyを2回以上観測した場合は、JSON decoderのlast-wins結果と先行表示が不一致になるため、
extractorはそのgenerationを早期停止してよい。同じ規則を`sufficiency`、`cited_refs`、`missing_aspects`を含む
全top-level keyへ適用してよいが、最終rejectの正本はEOF後のparserであり、extractor結果やlive配信状態へ
依存させない。nested object内の同名keyはtop-level重複とは数えず、nested object内の`answer` keyは本文fieldと
して扱わない。

JSON stringをdecodeした断片は、Direct packageへ依存せず共有位置の`AnswerVisibleTextFilter`へ渡す。このfilterが
chunkをまたぐcitation marker除去とouter whitespace処理を所有し、Direct / Evidenceでmarker文法を二重実装しない。

`sufficiency="insufficient"`のgenerationでも`answer`本文は通常どおり配信する。extractorは`sufficiency`の値や
field順序を待たず、最終statusはEOF後の既存validation / orchestrationが決める。

### 3. Retry and generation boundary

generationは同一worker attempt内の表示revisionを表す。初回provider attemptはgeneration 1、retryは
generation 2とする。

retryへ進む順序:

1. generation 1のpending coalescer bufferをabortし、未publish本文を破棄する。
2. `answer.reset { generation: 2 }`をcoalesceせずbest-effortでpublishする。
3. generation 2のprovider requestを開始する。
4. generation 2のdeltaだけを継続する。

reset publishがtimeoutしてもXADD成功の可能性があるため再送を正しさの前提にしない。consumerは大きい
generationのdelta自体をimplicit resetとして扱う。resetの成功確認を待たず、timeout / `None` / exception後も
直ちに次generationのprovider requestとdelta配信へ進む。resetだけのlazy retryは行わない。

現generationからvisible deltaが1件もpublishされていない場合も、retry / fallbackへ移るときは必ず次generationの
resetをpublishする。表示実績による条件分岐を持たず、すべてのrevision遷移を同じ状態機械で扱う。

### 4. Fallback revision

fallbackはAIの未検証出力ではなく、既存`EvidenceAnswerFlow`が所有する安全な固定本文である。fallbackへ進む
場合も表示内容の置換として扱い、現在generationをabortしてからgenerationを1増やす。

```text
generation 1 -> retry generation 2 -> fallback generation 3
generation 1 -> provider error -> fallback generation 2
```

新generationのresetをpublishした後、fallback本文を通常のdelta reporterへappend / finishする。frontendは
DB commitを待つ間も不正なAI下書きではなくfallbackを表示できる。fallback deltaの到達はDB永続化の条件に
しない。

### 5. Finalization and frontend independence

```text
provider stream EOF
  -> final JSON parse
  -> RawEvidenceAnswerDraft validation
  -> citation / evidence finalization
  -> AnswerQuestionResult assembly
  -> existing DB transaction commit
  -> terminal(completed)
  -> frontend / pollingがDB final answerを取得
```

workerはfrontendが全deltaを受信したか、DOMへ描画したか、EventSourceが接続中かを確認しない。
配信欠落時もDBへ完全な最終回答を保存する。terminalを取りこぼしたclientは既存pollingで同じDB結果へ
収束する。

`terminal(completed)`は「ライブ配信がすべて描画された」ではなく、「runの最終結果がDBへcommitされ、
frontendが確定結果を取得できる」ことを表す。

### 6. Reused live-update controls

- `AgentRunLiveAnswerDeltaReporter`のcoalescing / breakerを再利用する。
- `AgentRunExecutionProbe`を同じrun ID / attempt epochで再利用する。
- `AnswerGenerationStopped`をDirect固有contractから共有agent contractへ昇格し、両flowのworker routine returnで
  再利用する。EvidenceからDirect packageをimportしない。
- reset producerは同じ`AgentRunLiveStreamPublisher`を使い、別keyや別Streamを作らない。
- delta breakerがopenした場合、そのattemptではEvidence deltaも停止するが、全文生成とDB finalizationは継続する。

resetとdeltaは同じattempt-local breaker counterを共有する。

- reset / delta publishがnon-`None`のStream IDを返したら連続失敗数を0へ戻す。
- reset / delta publishが`None`またはexceptionなら連続失敗数を1増やす。
- 合計3回連続で未確認になったらbreakerをopenする。
- open後は同じattemptのreset / deltaをともに抑止し、half-openや自動復帰を行わない。
- `abort()`によるローカルbuffer / timer cleanupはopen後も行う。
- terminal publisherはbreaker対象外とする。

breaker openによりresetを送れない場合、clientに旧下書きが一時的に残ることを許容する。DB final answerと
terminal / polling置換が最終収束を保証し、breaker状態を回答生成の成否へ反映しない。

## Failure handling draft

| condition | live draft | generation / reset | final result |
|---|---|---|---|
| valid generation 1 | answerだけを表示 | generation 1 | strict draftをDB保存 |
| generation 1 validation failure | 旧draftを破棄 | reset 2後にretry | generation 2を検証 |
| generation 2 validation failure | generation 2を破棄 | reset 3後にfallbackを配信 | 既存fallbackをDB保存 |
| generation 1 provider error | partial draftを破棄 | reset 2後にfallbackを配信 | 既存fallbackをDB保存 |
| safety / recitation block | blocked chunkは送らない | retry / fallback規則に従う | 既存failure分類を維持 |
| malformed JSON / top-level重複key | 既配信分はretry時に破棄 | generation invalidとしてretry | retry後は既存fallback |
| MAX_TOKENSで不完全JSON | terminal後にfinal parseでreject | generation invalidとしてretry | retry後は既存fallback |
| terminal reasonなしEOF | partial draftを破棄 | provider errorとしてreset後fallback | 既存fallbackをDB保存 |
| Redis unavailable / breaker open | 欠落を許容 | runを失敗させない | 全文検証とDB保存を継続 |
| cancel / newer epoch | pendingを破棄 | resetを新規送信しない | routine stop、DB正本を維持 |

## Resolved generation and JSON decisions

保証条件とテスト表は次の決定を前提とする。

1. reset publishがtimeout / `None` / exceptionでも、lazy retryせず次generationへ進む。
2. object直下の全top-level重複keyをrejectする。nested objectの同名keyは別scopeとして扱う。
3. visible deltaが0件でも、retry / fallback時は常に次generationのresetをpublishする。
4. reset entryの到達は正しさの前提にせず、大きいgenerationのdeltaをimplicit resetとする。
5. reset / deltaは同じbreaker failure counterを共有し、open中は両方を抑止する。
6. top-level重複key検出の正本はEOF後の最終parserとし、extractor側検出は早期停止の最適化に限定する。

## Expected file changes

実装時の候補範囲を次に置く。実態確認で責務が異なる場合は、scopeを広げる前に仕様を更新する。

```text
backend/app/agent/answering/evidence_answer/contract.py
backend/app/agent/answering/evidence_answer/ai/gemini.py
backend/app/agent/answering/evidence_answer/flow.py
backend/app/agent/answering/evidence_answer/final_json.py (new)
backend/app/agent/answering/evidence_answer/json_answer_extractor.py (new)
  # provider stream、raw全文集約、重複keyを検出する最終parse、answer string増分復元、retry/reset lifecycle

backend/app/agent/contract.py
backend/app/agent/answering/visible_text.py (new/shared)
backend/app/agent/answering/direct_answer/contract.py
backend/app/agent/answering/direct_answer/stream_filter.py (move or compatibility re-export)
backend/app/agent/answering/direct_answer/flow.py
backend/app/agent/live_updates/answer_delta.py
backend/app/agent/composition.py
backend/app/queue/tasks/agent_run.py
  # shared visible filter / routine stop、reset-capable reporter、Evidence flowへの同一attempt配線

backend/tests/agent/answering/evidence_answer/ai/test_gemini.py
backend/tests/agent/answering/evidence_answer/test_flow.py
backend/tests/agent/answering/evidence_answer/test_json_answer_extractor.py (new)
backend/tests/agent/answering/evidence_answer/test_final_json.py (new)
backend/tests/agent/answering/direct_answer/test_flow.py
backend/tests/agent/answering/direct_answer/test_stream_filter.py
backend/tests/agent/live_updates/test_answer_delta.py
backend/tests/agent/live_updates/test_answer_delta_integration.py
backend/tests/agent/test_agent_run_task.py

backend/specs/agent-answer-streaming-sse.md
backend/specs/agent-direct-answer-deltas-slice.md
  # generation共通定義、共有部品の移動、Evidence pathの確定契約とslice進行状況
```

DB schema、SQLAlchemy model、Alembic migration、FastAPI / Pydantic response schema、frontend generated type、
新規dependencyを変更しない。必要になった場合は本sliceを停止し、Ask First対象として再判断する。

## Initial test plan

保証条件の確定後、少なくとも次の分類を具体的な入力・期待値へ落とす。

### Incremental JSON extraction

- answer以外のJSON構文・field・値をdeltaへ出さない。
- 全chunk分割でdecode済みanswerの連結が
  `_CITATION_MARKER_RE.sub("", EvidenceAnswerDraft.answer).strip()`と一致する。
- JSON escape、Unicode、surrogate pair、field順序、nested値、文字列内delimiterを扱う。
- malformed JSONと全top-level重複keyを最終parserがrejectし、reporterなし / breaker openでも結果が変わらない。
- extractorの重複key早期検知有無で最終typed errorとretry分類が変わらない。
- top-level重複keyが固定defect codeの`EvidenceAnswerDraftGenerationInvalidError`となる。
- outer whitespaceだけのanswerからvisible deltaを作らない。
- chunkをまたぐ完全な`[[N]]`だけを除去し、malformed markerは通常本文として残す。

### Evidence flow lifecycle

- generation 1成功、generation 1失敗からgeneration 2成功、2回失敗からgeneration 3 fallbackを固定する。
- generation 1 provider errorからgeneration 2 fallbackへ進む経路を固定する。
- retry前にpendingをabortし、reset 2より前のgeneration 2 deltaを許さない。
- visible delta 0件でもresetを送り、reset失敗後は待たずに次generationへ進む。
- provider error、block、parse error、validation errorでiterator / timerを回収する。
- STOP、完全JSONのMAX_TOKENS、不完全JSONのMAX_TOKENS、terminal reasonなしEOFを別々に分類する。
- `sufficiency="insufficient"`でもanswer本文を通常どおり配信する。
- audit、metric、previous error、defect repair、fallbackの既存契約を維持する。

### Worker and Redis boundary

- 同じrun / attempt epochのreporterとcontinuationをEvidence flowへ注入する。
- 実Redisでattempt.started、reset、generation 2 deltaがStream ID順にdecodeできる。
- reset喪失を模したconsumer contractで、大きいgeneration deltaが旧draftを破棄する。
- reset / deltaの成功・`None`・exceptionを同一counterで数え、open後は両方を抑止する。
- backend producer testは、同一generationのresetを複数回送っても指定generationを保持し、
  producer側で境界を勝手に増やさないことを固定する。
- Redis breaker openでも最終DB保存とterminal試行を継続する。
- cancel / epoch前進時にcomplete / mark_failed / terminal再publishを行わない。
- testは所有するUUID keyだけを削除し、`FLUSHDB`しない。

### Persistence and frontend independence

- 全provider outputとvalidation完了前にassistant messageを保存しない。
- frontend接続・delta受信・描画ackなしでもDB finalizationが完了する。
- terminal後のDB結果が、正常・retry・fallbackの各最終回答を完全に表す。

### Downstream Research UI consumer contract

- 現在より大きいgenerationのreset / deltaは、event適用前に旧draftを破棄する。
- 同一generationの重複resetでは、現在の正しいdraftを破棄しない。
- 小さいgenerationの遅延reset / deltaを無視する。
- Invariant 9の「同一generationの重複resetで現在draftを破棄しない」consumer testは、後続Research UI
  sliceが所有する。本backend sliceは、重複resetが同じgenerationを保持する入力event契約までを固定する。

## Implementation order

1. 確定したgeneration / JSON規則を保証条件と具体的なテスト表へ落とす。
2. incremental JSON extractorの同値性testを先に作る。
3. Gemini Evidence adapterをstream contractへ移す。
4. Evidence flowへraw全文集約、extractor、retry/reset、continuationを接続する。
5. reset-capable reporterを既存coalescer / breakerへ統合する。
6. composition / workerへ同じattemptのEvidence reporter / probeを接続する。
7. 実Redis producer→reader、DB persistence、routine stopを統合検証する。
8. backend checkを実行し、親仕様をResearch UI sliceへ更新する。

## Acceptance summary

本sliceの中心は、JSONを早く見せることではない。JSONという内部transportをfrontendから隠したまま、
表示可能な`answer`文字列だけを下書きとして早く届け、retry時には旧下書きを確実に捨てることである。

回答の正しさは、AI出力全体の受信後に行う既存schema・citation・evidence検証とPostgres永続化が所有する。
SSEの到達やfrontend描画はbest-effortであり、run完了の条件にはしない。
