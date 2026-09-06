# Outbox配信管理リポジトリ仕様

## 目的と実装範囲

保存済みのOutboxイベントをSQSへ配信するために、送信対象の確保と配信結果の記録を担うリポジトリを実装する。

既存の `backend/app/models/outbox_event.py` と `z21_outbox_events` migrationを利用する。新しいDBスキーマ、依存パッケージ、APIは追加しない。

実装先は `backend/app/outbox/repository.py`、リポジトリ名は `OutboxDeliveryRepository` とする。session終了後にも利用できる確保結果の型 `ClaimedOutboxEvent` を同じモジュールに定義する。

次は今回の対象外とする。

- SQS通信、ポーリングの起動、Lambdaやその他AWSリソースの構築。
- バックオフ計算、最大試行回数、停止判断、停止理由コードの一覧。
- 停止したイベントの再開、lease延長、保存済みイベントの削除。
- イベント記録側や、既存の後続タスク起動処理の変更。

## 責務と不変条件

- リポジトリは `AsyncSession` を受け取り、DB操作を行う。commit・rollbackは呼び出し元が管理する。
- サービスがlease期間・再試行の待ち時間・停止理由を決め、リポジトリに渡す。
- 配信管理の現在時刻とUUID生成はDBに統一する。
- イベント本体である `event_id`, `event_type`, `schema_version`, `payload`, `occurred_at` は変更しない。
- 同時に確保を行うワーカーは、ロック中の同じイベントを重複確保しない。
- 再確保後の古いtokenによる更新、および期限切れのleaseによる更新を拒否する。
- 配信成功・再試行予約・配信停止のいずれも、leaseの2列を同時にNULLにする。

将来のサービスは「確保してcommit → SQS送信 → 結果を記録してcommit」の順で呼び出す。SQS通信中にDBトランザクションを保持しない。

このleaseはDB更新の所有権を管理する。SQS送信後のDB記録失敗などによる重複送信そのものを防ぐ契約ではなく、再送でも同じ `event_id` を使用する。

## 時刻・UUID・試行回数

### DB時刻

対象選定、lease期限判定、期限計算、成功・停止時刻の記録には `statement_timestamp()` を使用する。これはSQL文の開始時刻であり、トランザクション開始時刻を返す `now()` と区別する。[PostgreSQLの時刻関数](https://www.postgresql.org/docs/current/functions-datetime.html#FUNCTIONS-DATETIME-CURRENT)

各公開操作を単一のSQL文で行い、その操作内の判定と記録に同じ基準時刻を使用する。呼び出し元から現在時刻や絶対期限を受け取らず、アプリ側の時計も使用しない。既存テーブルのINSERT時のDB初期値は変更しない。

leaseの有効性はSQL文開始時点で判定する。ロック待ちや通信を含む処理完了時刻の保証とはしない。呼び出し元は短いトランザクションで各操作を確定する。

### UUID

確保する行ごとにDBの `gen_random_uuid()` で新しい `lease_token` を生成し、`RETURNING` で取得する。アプリ側で生成せず、同じイベントの再確保でもtokenを更新する。[PostgreSQLのUUID関数](https://www.postgresql.org/docs/current/functions-uuid.html)

### 試行回数

`attempt_count` は「確保して開始した送信試行の回数」とする。確保時にDBで1加算し、成功・再試行予約・停止時には加算しない。

確保のcommit直後にプロセスが停止し、SQSに到達しなかった場合も数える。AWS SDK内部のリトライ回数は含めない。確保をrollbackした場合、加算も取り消される。

## 公開操作

引数はキーワード引数とする。期間には `datetime.timedelta`、IDとtokenには `uuid.UUID` を用いる。

| 操作 | 引数 | 戻り値 |
|---|---|---|
| `claim_ready_batch` | `limit: int`, `lease_duration: timedelta` | `list[ClaimedOutboxEvent]` |
| `mark_published` | `event_id: UUID`, `lease_token: UUID` | `bool` |
| `schedule_retry` | `event_id: UUID`, `lease_token: UUID`, `retry_delay: timedelta` | `bool` |
| `stop_delivery` | `event_id: UUID`, `lease_token: UUID`, `reason: str` | `bool` |

### 送信対象の確保

`lease_duration` は正の期間とし、ゼロ・負数は `ValueError` とする。期間の検証後、`limit <= 0` ならDB操作を行わず空リストを返す。

対象条件は次のすべてを満たすこととする。

```sql
published_at IS NULL
AND delivery_stopped_at IS NULL
AND next_attempt_at <= statement_timestamp()
AND (leased_until IS NULL OR leased_until <= statement_timestamp())
```

既存のDB制約により、lease未設定の場合はtokenと期限の両方がNULLである。

候補SELECTをCTEにし、対象条件を満たす行を `limit` 件まで `FOR UPDATE SKIP LOCKED` で確保する。その候補を同一SQL文のUPDATEで更新し、必要な列を `RETURNING` で返す。行ごとの追加クエリは発行しない。ロック中の候補は待機せずスキップする。[PostgreSQLの行ロック](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE)

更新する値は次のとおり。

- `lease_token = gen_random_uuid()`
- `leased_until = statement_timestamp() + lease_duration`
- `attempt_count = attempt_count + 1`

その他の列は維持する。対象がなければ空リストを返す。

候補の選択順序と返却リストの順序は保証しない。順序を保証するためのソートは不要とし、選択優先順位・返却順序・順序の不定性を検証するテストも追加しない。

### 確保結果

`ClaimedOutboxEvent` はORMインスタンスではなく、確保後の値を持つdataclassとする。session終了後に遅延ロードせず読み取れること。

| 項目 | 型 |
|---|---|
| `event_id` | `UUID` |
| `event_type` | `str` |
| `schema_version` | `int` |
| `payload` | `dict[str, Any]` |
| `occurred_at` | timezone付き `datetime` |
| `attempt_count` | `int` |
| `lease_token` | `UUID` |
| `leased_until` | timezone付き `datetime` |

`attempt_count` は加算後の値を返す。呼び出し元は確保トランザクションのcommit成功後に、この結果を送信に使用する。

### 成功・再試行・停止の共通更新条件

3操作は、単一の条件付きUPDATEで以下をすべて確認する。

```sql
event_id = :event_id
AND lease_token = :lease_token
AND leased_until > statement_timestamp()
AND published_at IS NULL
AND delivery_stopped_at IS NULL
```

更新できた場合は `True`、対象なし・token不一致・期限切れ・確定済みの場合は `False` とする。理由を区別するための追加SELECTは行わない。`True` は当該トランザクション内での更新成功を示し、commit完了を意味しない。

DB障害や制約違反は握りつぶさず例外を伝播させる。

| 操作 | 更新内容 |
|---|---|
| 成功 | `published_at = statement_timestamp()` |
| 再試行 | `next_attempt_at = statement_timestamp() + retry_delay` |
| 停止 | `delivery_stopped_at = statement_timestamp()`, `delivery_stop_reason = reason` |

いずれも `lease_token`, `leased_until` をNULLにし、それ以外の列は維持する。成功記録はSQS送信成功の記録であり、後続処理の完了を意味しない。

`retry_delay` はゼロ以上とし、負数は `ValueError` とする。ゼロは即時再試行可能を意味する。`reason` は空文字・空白のみを `ValueError` で拒否し、有効な文字列はそのまま保存する。理由コードの列挙やエラー全文の保存は行わない。

## テストと完了条件

`backend/tests/outbox/` に実PostgreSQLを使うテストを追加し、確保・配信結果の更新・トランザクションごとにまとめる。DB操作はモックせず、既存の隔離DBとsession fixtureを利用する。

確認する不変条件は次のとおり。

- 未送信・未停止・再試行可能・lease未設定または期限切れの行だけが確保される。
- 件数制限を守り、確保した行が対象条件を満たす。対象が上限を超える場合、選ばれる特定のIDは固定しない。返却結果はIDの集合や対応表で比較し、リストの順序をassertしない。
- 確保後にtokenと期限が設定され、試行回数が1増える。再確保でtokenが変わる。
- 独立した2つのsessionで、先行トランザクションが確保した行を後続がスキップする。ロックを保持した状態で後続を実行し、重複しないことを確認する。
- 成功・再試行・停止が指定の列を更新してleaseを解除し、イベント本体を維持する。
- 古いtoken、期限切れ、送信済み、配信停止済み、存在しないIDでは更新できない。
- 確保・状態更新はcommitで残り、rollbackで取り消される。repository内部でcommitしないことを確認する。
- 不正な期間・停止理由と、`limit <= 0`・再試行待ち時間ゼロを扱える。
- 記録時刻と期限はDB時刻および渡した期間に対応する。アプリの現在時刻を期待値にしない。

対象選定のテストには、十分過去・未来の固定日時を使用し、行の条件をテスト本体に明示する。厳密な時刻一致の境界までは検証しない。DB時刻の取得は、生成・記録された時刻を検証するテストに限定する。長いsleepや時計のモックに依存させない。境界の仕様は「再試行可能・期限切れは `<=`、有効なleaseは `>`」とする。

実装タスクは4操作と上記テストの追加、差分確認までを対象とする。ユーザーの検証保留指示が継続しているため、テスト・lintは実行せず、未検証であることを報告する。検証再開後に実PostgreSQLでのテストと `/check` を実行する。

この仕様書作成タスクではコード・テスト・DBを変更せず、コミット・PR作成も行わない。
