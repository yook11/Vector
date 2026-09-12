# 記事単位AI分析の資源管理とEmbeddingのセッション境界

## 現在の保証配置（2026-09-13）

- 共通資源は`backend/local_tests/test_article_analysis_lifecycle.py`で`open_article_analysis_consumer`を直接検証する。工程別handlerやSDKを切り替えず、実Engine・実DBと開閉観測用のAI context managerを使う。
- 準備順・所有期間・逆順解放・接続回収・呼び出し間の分離を一箇所で検証し、旧工程別`test_invocation_resources.py`は削除した。
- AI待機前の接続返却はConsumerの責任として`embedding/test_session_boundaries.py`へ残す。
- HTTP失敗とDB待機期限切れの保存・再処理は`embedding/test_event_processing.py`へ移し、資源解放のassertは共通テストへ集約した。
- プール再利用・ロールバック・タイムアウト・切断後の回復を共通ローカルテストへ移し、通常のDB部品テストに重複させない。SDK内部の終了方法と終了処理自体の障害は各部品の単体テストが担当する。

以下は共通化前の実装・検証記録であり、現在の配置は上記を正本とする。

## Problem

全体動作テストに監査・重複抑止・部分失敗が混在していたため、実際のハンドラー呼び出しが保存を確定してDBリソースを解放する保証に絞る。

## Evidence

- `backend/app/lambda_handlers/embedding/handler.py`: 呼び出し終了まで資源を管理する入口。
- `backend/app/lambda_handlers/embedding/resources.py`: Engineのdisposeを終了スタックへ登録する。
- 旧DB部品テスト: プール再利用・ロールバック・切断を確認していた保証は、上記の共通ローカルテストへ移した。
- `backend/local_tests/database.py`: migrationと実ロールを適用した独立DBを用意する。

## Invariants

- 製品のhandler・SDK・Consumer・Repository・Engine・disposeを使い、DB操作・終了処理をモックしない。
- テスト用Settings、IAM署名、SSM取得、Gemini HTTP応答を差し替える。DB待機ケースでは実timeoutの期限到来も制御する。
- 管理者はデータ準備用とし、処理はvector_appの接続設定を使う。
- 各呼び出しのEngine観測結果は1件を要求してpopで取り出し、配列の位置と呼び出し回数を対応づけない。
- Engine終了直前の貸出数が0で、応答時にはdisposeが完了している。
- 別接続から保存結果を確認し、使用した接続IDがpg_stat_activityから消えることを最大2秒の待機で確認する。
- 異なる記事を続けて処理し、両方で保存と接続解放を確認する。

## Non-goals

監査、重複抑止、エラー分類、部分失敗応答、プール設定の網羅、SDKの細かな終了順、製品コード・認証・schema変更、AWS/GCP実通信。

## Done

- `backend/local_tests/embedding/test_invocation_resources.py`で呼び出し終了時とAI待機中の接続管理を確認する。
- 旧`test_event_processing.py`の2ケースを置き換え、既存の部品テストは維持する。
- local一式・単体・既存DB結合・lint・formatを検証する。

## Verification

- ローカル45件、単体6,581件、既存DB結合1,400件が成功し、skipはない。
- 最終のEmbedding単独実行も1件成功。
- Ruff lint・formatと差分チェック成功。
- 実Engineのdisposeはインスタンス属性が読み取り専用のため、クラスメソッドをラップし、観測対象Engineに限って状態を記録する。元のdisposeは全呼び出しで実行する。

## AI待機中の接続返却

- Problem: Consumer単体の接続返却確認を、実SDKと実ハンドラーを通る待機状態の検証へ移す。
- Evidence: 旧`test_loads_once_and_closes_read_connection_before_ai`、Consumerの読み取りセッション境界、ServiceのAI呼び出し後の保存セッション。
- Invariants: HTTP応答を止め、到達合図を待ってから確認する。貸出接続0件、使用したDB接続がidleかつ未終了トランザクションなしであることを要求する。応答を再開すると保存・接続解放が成立する。
- Non-goals: タイムアウト動作、AI障害、監査、製品コードの変更。
- Done: 新しい待機中テストを追加し、既存テストは読み取り回数だけに限定する。固定sleepで待機状態を推測せず、確認が失敗してもHTTP応答を解放して呼び出し終了を待つ。

ハンドラーは別スレッドのイベントループで動くため、到達・再開の同期にはthreading.Eventを使う。到達待ち5秒、応答停止10秒、終了待ち15秒はテストが停止し続けることを防ぐ上限で、製品のタイムアウト契約ではない。

AI待機中テスト追加後の検証: ローカル46件（10.78秒）、単体6,581件、既存DB結合1,400件が成功。skipなし。製品コード・localテスト・変更したConsumerテストのRuff lint・formatと差分チェックが成功。

## AI通信失敗後の接続解放

- Problem: 正常終了と待機中に加え、実際のSDKで通信例外が起きた呼び出しの接続解放を保証する。
- Evidence: HTTPの共通差し替え、製品handlerの失敗応答と資源管理、既存Consumerのエラー分類・バッチ継続テスト。
- Invariants: HTTP境界でConnectErrorを発生させ、SDK・Consumer・後処理・実DB・disposeは製品コードを使う。HTTP呼び出し到達、対象行の未保存、失敗ID応答、貸出数0・dispose完了・実DB接続終了を確認する。続く別記事の呼び出しでは通信を正常に戻し、保存と解放を確認する。
- Non-goals: HTTP番号の網羅、エラー分類、監査、タイムアウト、製品変更。
- Done: `test_handler_releases_connections_after_ai_network_failure`を追加し、local一式・既存単体・結合・lint・formatを検証する。

通信失敗テスト追加後の検証: ローカル47件（11.07秒）、単体6,581件、既存DB結合1,400件が成功し、skipなし。Ruff lint・formatと差分チェックも成功。

## DB操作中の期限切れ

- Problem: 貸出中の実DB接続がロック待ちのまま処理期限を迎えた場合の解放を確認する。
- Evidence: Consumerのasyncio.timeout、Engineのcommand_timeout=5、Repositoryの保存前SELECT FOR UPDATE、既存のプール観測。
- Invariants: 別接続で対象行をロックし、pg_blocking_pidsで待機を確認してから元のasyncio.Timeoutの期限を所有ループ上でrescheduleする。DB操作・例外処理・disposeは実物を使い、timeout.expired()でConsumer期限の発火を確認する。失敗応答後もブロッカーを保持したまま未保存と接続解放を確認し、その後ロックを解除して同じ記事を正常保存する。
- Non-goals: DBドライバーの5秒タイムアウトの検証、期限秒数の検証、監査、製品コード・権限・schema変更。
- Done: `test_handler_releases_connections_after_database_operation_timeout`を追加し、ケース本体の保存状態確認と共通の解放確認を分離する。確認が失敗した場合もfinallyでロックを解除し、開始済み呼び出しの終了を待つ。

ロック到達待ち3秒と終了待ち15秒はテストの停止防止用であり、固定sleepでロック待ちと推測しない。次の呼び出し前に期限操作の差し替えも解除する。
APIは[Python 3.13 Timeout](https://docs.python.org/3.13/library/asyncio-task.html#asyncio.Timeout)と[PostgreSQL pg_blocking_pids](https://www.postgresql.org/docs/current/functions-info.html)に従う。
ユーザー指示により、この追加分のテストは実行しない。
