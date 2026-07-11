# Agent attempt epoch fencing token slice

Status: Implemented — 2026-07-11

## 位置づけ

この仕様は、Redis Stream配信基盤とSSE backend / BFF公開の間に置く前提sliceである。

```text
Slice 1   Redis Streamによるlive update配信基盤
Slice 1.5 attemptEpochの単調増加fencing token化（この仕様）
Slice 2   backend SSE endpoint / BFF proxy
```

このsliceでは、現在 `agent_runs.started_at` を流用している `attemptEpoch` を、DBが
原子的に採番する正の整数へ変更する。目的は時刻を管理することではなく、同じrunを実行する
複数workerのうち、どのattemptにeventを帰属させるかを順序付きの値で判定できるようにする
ことである。

DB schema / SQLAlchemy modelを変更するため、実装着手前に承認を得る。本文はその判断と
実装範囲を固定するための仕様書であり、この文書の作成自体はmigrationの適用を含まない。

## Problem

現在のlive update transportは、run取得時の `started_at` を `attemptEpoch` としてRedisの
全eventへ保存する。この方式では同じattemptかどうかの等値比較はできるが、観測したeventが
要求中のattemptより古いか新しいかを、DBへ問い合わせずに判定できない。

この不足は次の問題を生む。

- 新attempt開始後も旧workerが生存してpublishする「ゾンビworker」を、append順序だけでは
  安全に除外できない。
- readerが新しいattemptを観測しても、その事実をcallerへ返す結果契約がない。
- timestampは一意性をアプリケーション時刻に依存し、attemptの順序を表す専用の値ではない。
- 後続のSSE接続が、同じattemptを読み続けるべきか、run contextをDBから取り直すべきかを
  明確に判断できない。

## Evidence

2026-07-11時点の実装を根拠とする。

- `backend/app/models/agent_run.py`
  - `AgentRun.started_at` はnullableなtimezone付きdatetimeである。
  - attempt専用のDB columnは存在しない。
- `backend/app/agent/runs/repository.py`
  - `acquire_for_execution()` はactive runを `running` に更新し、`started_at = now` とする。
  - `PreparedAgentRun.attempt_epoch` には同じ `now` を返す。
- `backend/app/agent/runs/contracts.py`
  - `PreparedAgentRun.attempt_epoch` は `datetime` である。
- `backend/app/agent/live_updates/stream.py`
  - Redis envelopeとreader / publisherの `attempt_epoch` は `datetime` である。
  - readerは要求epochとの等値比較だけを行う。
  - 結果型には `stream_missing`、`attempt_absent`、`cursor_trimmed`、`unavailable`
    などがあるが、「より新しいepochを観測した」を表す状態がない。
- stale判定は `started_at` を使用している。これは経過時間の責務であり、attempt識別とは
  別に残す必要がある。
- Redis StreamのTTLは15分であり、旧形式entryは時間経過で自然に消える。

既存実装は移行元を示す証拠であり、このsliceの正しい契約そのものではない。

## Goals

1. runをworkerが取得するたび、DBがattempt番号を原子的に1増やす。
2. Redis上の全live eventを、正の整数 `attemptEpoch` へ帰属させる。
3. readerがepochの大小だけで、旧worker、現在attempt、新attemptを分類できるようにする。
4. readerが新attemptを観測した事実を、後続SSE層が安全に処理できる結果型で返す。
5. rolling deploy中に旧timestamp形式が混在しても、run本体や最終回答を壊さずlive表示だけを
   安全に劣化させる。

## Terminology

### attempt

workerが `acquire_for_execution()` に成功してから、その実行が終了するか再取得されるまでの
一回の実行単位である。同じrunがstale回収や再配送で再取得されると、新しいattemptになる。

### attemptEpoch

attemptを識別し、その新旧を比較するための単調増加整数である。時刻ではない。

- DB / Python field名: `attempt_epoch`
- Redis serialized field名: `attemptEpoch`
- DB上の型: `BIGINT`
- DB上の値域: `0` 以上
- Redis envelope上の値域: `1` 以上
- `0`: epoch対応codeによってattemptがまだ採番されていない
- `1`: 最初に取得されたattempt
- `2` 以降: 再取得のたびに1増える

例:

```text
queued       attempt_epoch = 0
worker A取得 attempt_epoch = 1
再配送       attempt_epoch = 2
再々配送     attempt_epoch = 3
```

値が大きいほど新しいattemptである。連続した値がreaderにすべて見えることは要求しない。
たとえばepoch 1を読んでいるreaderがepoch 3を観測した場合も、新attemptへの切替として扱う。

新codeだけが取得を行う定常状態では、active runについて `status = queued AND
attempt_epoch = 0` を「未取得」と判定できる。rolling deploy中は旧codeがepochを増やさず
取得する可能性があるため、`attempt_epoch = 0` 単独をqueued判定に使わない。

### fencing tokenとしての役割

`attemptEpoch` は、古いworkerの出力を新しいworkerの出力から締め出すためのfencing tokenで
ある。worker自体を強制停止する仕組みではない。旧workerがpublishを続けても、readerが
小さいepochを除外することでユーザーへ混在して見せない。

## Invariants

1. attemptの帰属は `attemptEpoch` で決める。Stream ID、markerの位置、append順序、
   `publishedAt` では決めない。
2. `acquire_for_execution()` が成功するたび、同じrunの `attempt_epoch` はDB内で原子的に
   1増える。
3. 同じrunの2回の成功した取得が、同じ `attempt_epoch` を受け取ることはない。
4. terminal runまたは存在しないrunの取得は成功せず、epochも増えない。
5. transactionがrollbackされた取得の増分は永続化されない。
6. `started_at` は取得時刻・stale判定のために残す。attempt識別には使わない。
7. Redisへpublishできるのは取得成功transactionのcommit後である。commit失敗時は
   `begin_attempt()` も後続publishも行わず、全envelopeの `attemptEpoch` は1以上である。
8. readerへ渡す要求epochは1以上であり、0以下はRedis操作前にcaller契約違反として拒否する。
9. readerは要求epochより小さいentryを旧attemptのeventとして捨て、cursorを前進させる。
10. readerは要求epochと等しいentryだけをeventとして返す。
11. readerは要求epochより大きいentryを観測したら、新attempt境界としてcallerへ通知する。
12. 新attempt境界となったentryは、古いattemptのreadで消費済みにしない。
13. 同じepochの `attempt.started` が重複しても、新attemptへの切替とは扱わない。
14. 壊れたentryや旧timestamp形式entryは返さず、他の有効entryの処理を妨げない。
15. live updateの欠落や形式移行は、DB上のrun状態、最終回答、既存polling結果を壊さない。

## Scope

### このsliceに含める

- `agent_runs.attempt_epoch` columnとDB制約の追加
- 既存rowのbackfill
- SQLAlchemy modelの更新
- `acquire_for_execution()` によるDB内の原子的な採番
- `PreparedAgentRun.attempt_epoch` の `datetime` から `int` への変更
- Redis envelope、publisher、readerのinteger epoch対応
- reader結果型への新attempt観測結果の追加
- rolling deploy時の旧timestamp entryの安全なskip
- 関連する親仕様・transport仕様・SSE draftの前提更新
- 上記を保証するmigration / repository / transportテスト

### このsliceに含めない

- backend SSE endpointの追加
- Next.js BFF proxyの追加
- EventSource client、画面表示、下書き管理
- `answer.delta` producerやLLM生成経路の配線
- attemptごとの監査履歴を永続保存する別table
- `started_at` の削除やstale sweepの変更
- 旧timestamp形式を新形式として読み直す互換decoder
- Redis Listの既存 `recentEvents` 履歴契約の変更
- frontendへ公開するAPI response shapeの変更
- SSE接続時のqueued待機とHTTP responseの実装

## DB design

### Column

`agent_runs` に次を追加する。

```text
attempt_epoch BIGINT NOT NULL DEFAULT 0
CONSTRAINT ck_agent_runs_attempt_epoch_nonnegative
  CHECK (attempt_epoch >= 0)
```

`BIGINT` はrunの現実的な再取得回数に対して十分な余裕があり、アプリケーション側では
Python `int` として扱う。このcolumn単体の検索は要件にないためindexは追加しない。

constant server default `0` は、新規queued runを「未取得」として構造的に表すため残す。
アプリケーションだけのdefaultにはしない。

### Backfill

既存rowは次の規則でbackfillする。

| 既存状態 | `attempt_epoch` |
|---|---:|
| `started_at IS NULL` | `0` |
| `started_at IS NOT NULL` | `1` |

過去に何回取得されたかは既存schemaから復元できない。backfillの目的は過去の正確なattempt
回数を再構築することではなく、migration適用時点の未取得と取得済みを区別し、以後の
epoch対応codeによる採番を単調増加にすることである。

terminal rowもこの規則でbackfillする。terminalになったこと自体ではepochを増やさない。

### Migration contract

- Alembic migrationだけでschemaを変更する。
- upgradeはcolumn追加、backfill、CHECK制約追加を行う。
- downgradeはCHECK制約を削除してからcolumnを削除する。
- migrationはDB transaction内で完結させる。
- upgrade / downgradeの先頭で `SET lock_timeout = '5s'` を設定し、table lock競合時は
  無期限に待たず失敗する。
- migration gateの分類と実装形式はrepoの現行規約に従う。
- production適用順は「migrationを先に適用し、その後に新codeをdeploy」とする。

## Acquire contract

`acquire_for_execution()` はattempt番号をPythonで読み、加算して保存してはならない。
同じSQL `UPDATE` の中で加算し、`RETURNING` で確定値を取得する。

概念上のSQLは次の通りである。

```sql
UPDATE agent_runs
SET status = 'running',
    started_at = :now,
    attempt_epoch = attempt_epoch + 1
WHERE id = :run_id
  AND status IN ('queued', 'running')
RETURNING attempt_epoch;
```

PostgreSQLが同一rowへの更新を直列化するため、同時取得でも各成功呼び出しは異なる連番を
受け取る。呼び出し側は実行順を仮定せず、返された値をそのattemptの唯一のepochとして使う。

更新結果が0 rowなら `None` を返し、publisherを作らない。更新に成功した場合だけ、
`RETURNING` で得た整数を `PreparedAgentRun.attempt_epoch` へ入れる。`now` や更新前のORM
instanceからepochを組み立てない。

`started_at = :now` は採番と同じUPDATEで更新する。これにより時刻管理とattempt採番は責務を
分けつつ、取得成功という同じtransaction境界に置かれる。

## Redis envelope contract

serialized field名 `attemptEpoch` は維持し、値の型だけをISO 8601 timestamp文字列から
正の整数へ変更する。

```text
type: event vocabulary
attemptEpoch: positive integer
payload: compact JSON string
publishedAt: publisherがISO 8601で書く診断用string
```

readerは `publishedAt` をopaque stringとして扱い、datetime parseしない。形式不良でも
attempt境界と有効payloadを失わせない。

Pydantic envelopeは `attemptEpoch >= 1` を検証する。Redis clientから得るfield値は文字列に
なるため、正の10進整数文字列は整数として受理する。一方、旧ISO timestamp、
小数、負数、0、真偽値相当、不正な文字列は有効epochとして扱わない。

全event typeに同じepochを付ける。`attempt.started` だけを境界のsource of truthにはしない。
markerのpublishがtimeoutした場合やtrimされた場合も、各event自身のepochで帰属を判定する。

## Reader result contract

### Input precondition

readerの `requested_attempt_epoch` は1以上を必須とする。0以下はcaller契約違反として
validation errorを返し、Redis commandを実行しない。queued runをreaderへ渡すかどうかは
後続SSE sliceの責務であり、このreaderはepoch未採番状態を暗黙に新attemptへ変換しない。

### Result vocabulary

既存の結果語彙へ次を追加する。

```text
ATTEMPT_ADVANCED = "attempt_advanced"
```

結果型へ次を追加する。

```text
observed_attempt_epoch: int | None
```

`ATTEMPT_ADVANCED` のときだけ `observed_attempt_epoch` は必須であり、要求epochより大きい。
その他のstatusでは `None` とする。

`ATTEMPT_ADVANCED` の結果契約は次の通りである。

```text
status = ATTEMPT_ADVANCED
events = ()
observed_attempt_epoch > requested_attempt_epoch
next_cursor = 新attempt境界を消費する直前のcursor
```

cursor付きreadでbatchの最初が新attempt境界なら、`next_cursor` は入力cursorと同じになる。
cursorなしの初回readで境界より前に消費可能entryがなければ、`next_cursor` は `None` とする。

後続SSE層はこのstatusを受けたら、固定済みの旧epochで読み続けない。SSE sliceでは
`observed_attempt_epoch` へ同一接続をre-pinし、`next_cursor` を維持して境界entryから再読する。
attempt取得transactionはRedis publishより先にcommitされるため、re-pinのためのDB再取得は
不要である。別consumerは安全側へ接続を閉じてもよいが、境界entryを旧cursorで消費してはならない。

### Epoch classification

要求epochを `R`、decodeできたentryのepochを `E` とする。

| 条件 | readerの処理 |
|---|---|
| decode不能 | entryをskipしcursorを進める |
| `E < R` | ゾンビworker / 旧attemptとしてskipしcursorを進める |
| `E = R` | 現attemptのeventとして返す |
| `E > R` | 新attempt境界としてreadを止め、`ATTEMPT_ADVANCED` を返す |

大小比較だけを使い、`R + 1 = E` は要求しない。

結果の返却では境界前の現attempt eventを優先する。`E > R` を観測しても、同じbatch内で
その境界より前に返却可能な `E = R` のeventがある場合は、先にそれらを `EVENTS` として
返す。境界entryは消費せず、次回readで `ATTEMPT_ADVANCED` を返す。

### Boundary and batch rules

1回のRedis readで現attempt eventと新attempt eventが同じbatchに入ることがある。

```text
epoch 1 event A
epoch 1 event B
epoch 3 event C
epoch 1 zombie event D
```

要求epochが1の場合、readerは次の順序で返す。

1. 最初の結果はAとBだけを `EVENTS` として返し、cursorはBまで進める。
2. 次のreadはCを新attempt境界として `ATTEMPT_ADVANCED` を返す。
3. CのStream IDを旧epochのcursorとして消費済みにしない。
4. Dはepoch 3のreaderから見れば小さいため、ゾンビeventとしてskipされる。

これにより、境界前に既に読めた現attempt eventを失わず、新attempt entryを旧attemptの
処理によって読み飛ばさない。

同一epochの `attempt.started` が複数存在する場合は通常の同epoch eventとして扱う。
consumerが下書きを破棄する条件もmarkerの出現回数ではなく、受信した任意eventのepochが
現在値より大きいことに限る。markerがtrim・publish喪失・decode不良でも、各event自身のepochを
境界として使う。

streamに要求epochより小さい有効entryしか存在しない場合は、それらをskipしてcursorを
最後のentryまで進め、`ATTEMPT_ABSENT` を返す。新しいraw entry自体が存在しない
`EMPTY` や、大きいepochを観測した `ATTEMPT_ADVANCED` とは区別する。

`ATTEMPT_ABSENT` はterminal・劣化シグナルではない。follow readでは返された
`next_cursor` から読み続け、後着の現epoch eventを受信する。初回readでもworker開始raceとして
有限時間待機し、statusだけを理由に接続を閉じない。

### Existing status semantics

このsliceで既存statusを統合しない。少なくとも次を区別したままにする。

| status | 意味 |
|---|---|
| `STREAM_MISSING` | Redis keyが存在しない |
| `ATTEMPT_ABSENT` | streamは存在するが、要求epochの有効eventがなく、より大きい有効epochも観測していない。接続は継続する |
| `EMPTY` | cursor以後に新しいentryがない |
| `CURSOR_TRIMMED` | cursorが保持範囲より古い |
| `UNAVAILABLE` | timeoutやRedis障害で読めない |
| `ATTEMPT_ADVANCED` | 要求epochより大きいepochを観測した |

markerがtrimされても、残っている各eventのinteger epochで帰属を判定する。そのため
「markerがない残存entryを同一attemptとみなす」という位置依存fallbackは採用しない。

cursor trimの検出契約、Stream IDの数値pair比較、Stream TTL、page size、per-operation timeout
は既存transport仕様を維持する。

## Rolling deployment

### Deploy order

1. `attempt_epoch` columnを追加するmigrationを適用する。
2. integer epochを採番・publish・readするbackend codeをdeployする。
3. Redis TTLが経過すると旧timestamp形式entryは自然に消える。

新codeをmigrationより先にdeployしてはならない。

### Old worker compatibility

deployが重なる間、旧workerはISO timestamp形式の `attemptEpoch` をRedisへ書く可能性がある。
新readerはinteger schemaに合わないentryとしてskipする。timestampを整数へ変換する互換処理は
入れない。

migration適用後から旧取得codeが停止するまでの間、旧codeは `started_at` を更新しても
`attempt_epoch` を増やさない。このため、取得済みのrunning / terminal runでも
`attempt_epoch = 0` のrowが生じ得る。この重複期間と、その期間に作られたhistorical terminal
rowについて、0は「一度も取得されていない」ではなく「epoch対応codeによる採番がない」を
意味するものとして受容する。

後続SSE層は `attempt_epoch = 0` だけでqueuedと判定せず、少なくとも
`status = queued AND attempt_epoch = 0` を使用する。旧取得codeの停止とactive runのdrain後は、
active runについてこの複合条件を未取得判定として利用できる。historical terminal rowを
厳密に1へ正規化することはこのsliceの要件にせず、必要になった場合は旧writer停止後の
reconciliationを別途仕様化する。

この期間はin-flight runのlive eventが一部または全部見えない可能性を受容する。ただし次は
維持する。

- DB上のrun状態遷移
- 最終回答のDB保存
- pollingによる最終状態・最終回答の取得
- Redis障害時と同じ安全なUI劣化

新旧形式を曖昧に受理してattemptを誤帰属させるより、live表示を欠落させる方を選ぶ。

## Required file changes

実装sliceで変更対象となるファイルを次に固定する。テストファイル名は既存配置に合わせて
確定してよいが、責務は広げない。

```text
backend/alembic/versions/<revision>_agent_runs_attempt_epoch.py
backend/app/models/agent_run.py
backend/app/agent/runs/contracts.py
backend/app/agent/runs/repository.py
backend/app/agent/live_updates/stream.py
backend/tests/.../migration tests
backend/tests/agent/runs/... repository tests
backend/tests/agent/live_updates/... stream tests
backend/tests/agent/test_agent_run_task.py
backend/specs/agent-answer-streaming-sse.md
backend/specs/agent-live-stream-transport-slice.md
backend/specs/agent-sse-backend-bff-slice.md
```

FastAPIの外部Pydantic schemaは変更しないため、このslice単独ではfrontend type再生成を
要求しない。実装時に外部schema変更が必要になった場合はscope changeとして止まり、別途判断する。

## Required tests

既存テストが同じ条件を明示的に保証している場合は拡張してよい。テスト名や期待値から条件を
確認できない場合は、新しいテストを追加する。「近い経路を通る」だけでは保証済みとみなさない。

### Migration / model

1. upgradeで `attempt_epoch BIGINT NOT NULL DEFAULT 0` とCHECK制約が作られる。
2. `started_at IS NULL` の既存rowは0へbackfillされる。
3. `started_at IS NOT NULL` の既存rowは1へbackfillされる。
4. DBは負の `attempt_epoch` を拒否する。
5. downgrade後にcolumnと制約がなくなり、再upgradeできる。
6. SQLAlchemy modelの型、nullability、server default、CHECK制約名がmigrationと一致する。
7. upgrade / downgrade / 再upgradeの各操作が `lock_timeout = 5s` を設定する。

### Repository acquisition

1. queued runの初回取得でepochが0から1になり、返却値とDB値が一致する。
2. running runの再取得でepochが1から2になる。
3. 同じrunへの同時取得が両方成功する場合、返るepochは重複しない連番になる。
   呼び出し完了順とepoch順の一致は仮定しない。
4. terminal run、存在しないrun、更新競合で取得できないrunはepochを増やさず `None` を返す。
5. transaction rollback後はincrementが永続化されない。
6. `PreparedAgentRun.attempt_epoch` は `RETURNING` の値であり、`started_at` から生成されない。
7. acquireが `None` の場合、workerは `begin_attempt()` も後続publishも行わない。この条件は
   worker taskレベルの既存 `backend/tests/agent/test_agent_run_task.py` を拡張して保証する。
8. 取得transactionのcommit完了後にだけ `begin_attempt()` を呼び、commit失敗時はpublisherを
   作らずRedisへ何もpublishしない。この順序は同一接続re-pinがDB再確認を省略する前提として、
   SSE sliceのworker境界testでも回帰を防ぐ。

### Redis publisher / envelope

1. 取得成功後の全event typeへ同じ正のinteger epochが入る。
2. epoch 0、負数、不正な文字列は有効envelopeとして扱われない。
3. 旧ISO timestamp形式entryはdecodeされずskipされる。
4. 同一epochの `attempt.started` がtimeout後のretryで二重appendされても、epoch境界は
   変わらない。

### Reader fencing behavior

1. 要求epochと等しいeventだけが返る。
2. epoch 2のmarker後にepoch 1のゾンビeventがappendされても、epoch 2の結果へ混ざらない。
3. 小さいepochをskipした後にcursorが前進し、同じentryを次回再読しない。
4. 要求epochより小さい有効entryしか存在しない場合は、最後のentryまでcursorを進めて
   `ATTEMPT_ABSENT` を返し、`EMPTY` や `ATTEMPT_ADVANCED` にはしない。
5. cursor付きfollow readで `ATTEMPT_ABSENT` を返した後、その `next_cursor` から再開して
   後着の現epoch eventを欠落なく返す。
6. epoch 1を要求中にepoch 3を観測すると、飛び番でも `ATTEMPT_ADVANCED` を返す。
7. `ATTEMPT_ADVANCED.observed_attempt_epoch` は観測値3であり、eventsは空である。
8. 新attempt境界entryを指すStream IDは旧attemptの `next_cursor` として消費されない。
9. 同batchにepoch 1のevent群とepoch 3のeventがある場合、epoch 1分を先に返し、次回readで
   `ATTEMPT_ADVANCED` を返す。
10. 旧epochのreadが `ATTEMPT_ADVANCED` を返した後、新epochかつcursorなしで再読すると、
   境界entryを含む新attemptの全eventが欠落なく返る。
11. 新attempt境界後に小さいepochのゾンビeventがあっても、新epochのreadでskipされる。
12. 同じepochの重複markerは `ATTEMPT_ADVANCED` にならない。
13. markerがtrimされても、残存eventのepochが一致すれば現attemptとして返る。
14. trim済みcursorは引き続き `CURSOR_TRIMMED` になり、epoch分類と混同しない。
15. marker payloadまたは `publishedAt` が壊れても、共通epochだけで境界を判定できる。
16. Stream IDは文字列の辞書順でなく数値pairで比較し、`9-x < 10-x`、`1-9 < 1-10`
    が成立する。
17. `requested_attempt_epoch = 0` または負数はvalidation errorになり、Redis commandを
    実行しない。

### Degradation / regression

1. stream keyが存在し、旧timestamp entryだけが存在する場合は `ATTEMPT_ABSENT` を返す。
2. stream key自体が存在しない場合は `STREAM_MISSING` を返す。
3. live entryをskipしてもDB上のterminal結果とpolling応答は変わらない。
4. StreamのMAXLEN、TTL、page size、timeoutの既存保証が維持される。
5. `status = queued AND attempt_epoch = 0` のrunからpublisherは作られない。

## Implementation order

1. migrationとmodelを追加し、backfill / constraintテストを書く。
2. repositoryを原子的increment + `RETURNING` へ変更し、取得競合テストを書く。
3. `PreparedAgentRun` とpublisher / envelopeをinteger epochへ変更する。
4. reader入口へ1以上のvalidationを追加し、結果型へ `ATTEMPT_ADVANCED` と
   `observed_attempt_epoch` を追加する。
5. readerを大小比較と境界非消費の規則へ変更し、fencingテストを書く。
6. worker接続の負パスとrolling deploy劣化をテストする。
7. 関連3仕様書のtimestamp前提とqueued判定を更新する。
8. migration、対象unit / integration test、lint、format、type checkを実行する。

手順2のDB採番を完了する前にRedis側だけintegerへ変更してはならない。採番source of truthがない
状態を作らないためである。

## Verification

実装後はrepoのcheck手順に従い、少なくとも次を確認する。

- Alembicのsingle headとmigration gate
- migration upgrade / downgrade / backfill
- modelとmigrationのschema整合
- repository unit / DB integration tests
- live stream unit / Redis integration tests
- backend lint / format / type check
- agent run既存回帰テスト

実RedisまたはPostgreSQLが必要な検証を実行できない場合は、未実行項目と理由を明記し、Doneと
みなさない。

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Pythonでread-modify-writeしてepochが衝突する | DB内increment + `RETURNING` のみを許可する |
| migration前に新codeが動く | migration firstのdeploy順を固定する |
| rolling deploy中のepoch 0をqueuedと誤認する | statusとの複合条件を使い、0単独で判定しない |
| 旧timestamp entryを新形式と誤認する | integer schemaで厳格にrejectし、live表示だけ劣化させる |
| marker欠落・trimで境界を失う | 全envelopeと全公開eventのepochを比較し、marker位置に依存しない |
| 新attempt entryを旧readが読み飛ばす | `ATTEMPT_ADVANCED` 時は境界entryをcursor消費しない |
| ゾンビworkerが新marker後にpublishする | 小さいepochを常にskipしてcursorを進める |
| epochが飛んだとき切替できない | 連続性を要求せず `observed > requested` で切り替える |
| epochを時刻用途にも流用する | stale判定は `started_at`、attempt帰属は `attempt_epoch` に分離する |
| integerの外部公開が意図せず破壊的変更になる | このsliceは内部DB / Redis契約に限定し、外部schema変更時は停止する |

## Done

次の全条件を満たしたとき、このsliceは完了する。

1. `agent_runs.attempt_epoch` がDB制約付きで存在し、既存rowが規則どおりbackfillされている。
2. 成功したrun取得ごとにDBがepochを原子的にincrementし、確定値をworkerへ返す。
3. Redisの全live eventが正のinteger epochを持つ。
4. readerが0以下の要求epochをRedis操作前に拒否し、小さい・等しい・大きいepochを
   仕様どおり分類する。
5. `ATTEMPT_ADVANCED` と境界非消費の結果契約が実装・テストされている。
6. ゾンビworker、飛び番、重複marker、trim、壊れたentry、旧timestamp entryの必須テストが
   通っている。
7. `started_at` によるstale判定と既存run lifecycleに回帰がない。
8. 関連する3仕様書からtimestamp epoch前提が除かれ、SSE sliceがinteger epochを前提に
   実装可能な状態になっている。
9. 必須verificationが成功し、未解決のschema / reader contract判断が残っていない。

このDoneを満たした後に、backend SSE endpoint / BFF proxy sliceへ進む。
