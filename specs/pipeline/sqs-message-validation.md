# SQSメッセージの形式検証と工程別handlerの配置

## Problem

SQSメッセージの形式検証を共通で使い、各工程の処理をその場で読める構成にする。反復・1件処理・診断まで共通関数へ渡す設計は、利用者の意図を超えて呼び出しを複雑にしていたため撤去する。

## Evidence

- 既存のlambda_handlers/sqs/records.pyにSqsRecordBatchとSqsRecordがあり、両handlerはこの形式検証を共有している。
- assessment-consumer.mdとembedding-consumer.mdは、全ID検証の後に本文を個別検証し、構造不正と個別失敗の扱いを分ける。
- Assessmentは工程別compositionでConsumerを準備・解放している。Embeddingの準備・解放も同じ配置へ移すことが承認されている。

## Invariants

- 全IDを本文より先に検証し、配送構造・ID不正は全件未処理のまま伝播する。
- bodyの欠落・型不正は該当メッセージだけの失敗とする。
- レコード構築は本文の存在・文字列型までを検証する。JSON解析はparse_jsonが担当し、イベント種別・payloadの検証は工程の契約へ委ねる。
- 工程別の逐次実行・失敗IDの原文と入力順・空バッチ応答・診断・キャンセル伝播を維持する。
- 資源の初期化後に入力を検証し、同一呼び出し内でConsumerを共有する。取得済み資源は逆順に解放し、利用側の例外を初期化失敗として再記録しない。

## Design

- SqsRecordBatch.from_lambda_eventは配送構造と全IDを検証し、本文未検証のSqsRecordInputを保持する。各入力のto_recordは本文の存在・文字列型を検証して、body: strを持つSqsRecordを返す。SqsRecord.parse_jsonは本文のJSON解析だけを担当する。
- 各_run_*はこれらを直接呼び、forループ内にイベント解析・Consumer実行・診断・失敗ID集約を記述する。
- process_sqs_batch、SqsBatchDiagnostics、partial、コールバック用の_process_messageは設けない。応答のTypedDictは従来どおり各handlerが持つ。
- Consumerの準備・解放は工程別compositionが所有する。Embeddingのopen_embedding_consumerは既存のresources・gemini_client・consumerの初期化順、診断段階と通常例外・キャンセルの扱いを維持する。

## Non-goals

反復・1件処理・診断の共通化、新しい抽象基盤、Outbox relay・SQS送信、イベント契約、Consumer本体、DB schema、認証・認可、依存、AWS設定・デプロイは変更しない。

## Done

各handlerが共通の形式検証を直接使い、工程固有の処理を追える構成にする。形式検証の詳細はsqs/test_records.py、工程別の制御・応答・診断は各handlerテスト、資源の配線・初期化・終了はcompositionテストが担当する。ループは工程別に実装するため、その制御テストも各工程に残す。

Ruff lint・format、全単体テストとmake test-integrationで検証する。migration適用済みDBのlocal_tests・実AWS試験・デプロイは今回の検証範囲に含めない。

## 検証結果（2026-09-12）

- app全体と変更テストのRuff lint・format確認、git diff --checkが成功した。
- 全単体テスト6,634件が成功した。
- `make test-integration TEST_COMPOSE_PROJECT=vector-test-sqs-validation-20260912 PYTEST_ARGS='-x -q'`で統合テスト1,400件が成功し、一時DB・Redis・ネットワークの削除まで確認した。
- 既存の非推奨・Logfire関連の警告は残る。検証範囲外のlocal_tests・実AWS試験・デプロイは未実施。
- 形式検証は既存部品の直接利用に戻し、共通バッチ実行関数・診断Protocol・コールバックを撤去した。工程別compositionへの配置統一は維持した。


## イベント契約とSQS受信の責務整理（2026-09-14）

補完・Curation・Assessment・Embeddingの4受信工程で、イベント契約と配送処理を分離した。イベント型は`from_input(data)`で入力を検証し、型内部の変換処理で固定の理由・項目・コードを返す。検証例外の`invalid`は`reason`と`issues`を保持し、入力値・未知項目名・元のPydantic例外の原因チェーンを引き継がない。

| イベント | 契約の所有先 |
|---|---|
| `article.incomplete_recorded` | `collection/article_acquisition/events.py` |
| `article.analyzable_created` | `collection/events.py`（取得・補完の共有契約） |
| `article.curated_signal` | `analysis/curation/events.py` |
| `article.assessed_in_scope` | `analysis/assessment/events.py` |

工程別Lambdaの解析関数はJSON解析後にイベント型の`from_input`を呼び、イベント検証例外をそのまま伝える。JSON構文・重複キー・非標準数値の拒否はLambda側に残し、工程別の`MessageJsonInvalidError`で表す。Lambda側に同じイベント違反理由を再定義しない。

イベント検証例外・JSON解析例外・共通の`SqsInputError`は通常の`Exception`を継承し、`VectorDomainError`・`SAFE_ATTRS`に依存しない。配送recorderがログ名・理由・検証詳細を選択し、JSON解析失敗では従来どおり`reason="invalid_json"`と空の`issues`を記録する。ログ障害で配送結果を変更しない。

全IDの事前検証、逐次処理、個別入力不正の再配信、後続処理の継続、キャンセル伝播は維持した。既存Outbox送信も同じイベント契約を使用し、送信本文・日時表現・送信失敗への変換を維持する。Consumer・DB schema・依存・AWS設定・Outboxの例外階層は変更しない。

形式検証のテストはイベント所有工程、JSON解析と例外の伝播はLambdaの解析テスト、再配信と配送診断は各handlerテストに置く。補完の実handler・実Consumer・実DBの保証は`local_tests/completion/test_completion_delivery.py`に維持する。

検証結果（2026-09-14）: app全体と変更テストのRuff lint・format、全単体7,078件、`make test-integration`のDB統合1,481件、`make test-local`のローカル111件が成功した。`git diff --check`と一時DB・Redis・ネットワーク・ボリュームの削除を確認した。既存の非推奨・Logfire関連の警告は残る。AWS実接続・設定適用、可視性変更・残り時間の検証はスライス4・5の対象として未実施。

イベント不正の型名は`EventInvalid`・`EventInvalidError`・`EventInvalidReason`・`EventInvalidIssue`・`EventInvalidField`・`EventInvalidCode`に統一する。例外は`invalid`に不正情報を保持し、Pydanticの`ValidationError`からの変換はイベント型の`_event_invalid_from_validation_error`が担当する。Outboxが受け取る検証詳細のProtocolも`EventInvalidIssue`とし、分類条件・ログ・配送応答は変更しない。

EventInvalidへの命名統一後の最終検証（2026-09-14）: コミット対象だけを反映した検証用チェックアウトで、app全体・変更PythonファイルのRuff lint・format、全単体7,048件、DB統合1,475件、ローカル110件が成功した。別件の未コミットテスト整理は含めていない。差分チェックと一時DB・Redis・ネットワーク・ボリュームの削除を確認した。AWS実接続・設定適用、個別待機・残り時間管理はスライス4・5に残る。


## レコード構築とJSON解析の分離（2026-09-23）

Problem: 本文の型検証をJSON解析から分け、工程ごとに重複したJSON解析ヘルパーを共通SQS境界へ集約する。

Evidence: 取得・補完・Curation・Assessment・Embeddingの5工程がSqsRecordBatchを共有している。AssessmentのApplicationErrorと文脈付きログ、補完の残り時間確認・可視性変更は最新mainの動作を維持する。

- `SqsRecordInput.from_lambda_record`は受信データの構造とIDを確認し、本文不正の診断を各レコードの処理まで遅らせる。`SqsRecordInput`は本文未検証の入力、`SqsRecord`は本文の文字列型を確認済みのレコードとして区別する。
- `record_input.to_record()`が本文の欠落・型不正を`SqsInputError`で拒否する。JSON文字列として正しいかはレコードの成立条件に含めない。
- `parsed_body = record.parse_json()`がJSON構文、全階層の重複キー、NaN・Infinity・-Infinityを検査する。解析失敗は本文と元例外チェーンを持たない`SqsMessageJsonInvalidError`とし、ApplicationErrorの固定診断理由`invalid_json`を保持する。
- JSONルートはオブジェクトに限定しない。イベント型の`from_input(parsed_body)`がイベント契約を検証する。取得工程は既存の`acquisition_request_from_message(parsed_body)`へ渡す。
- 工程別のevent.pyと取得工程のmessage.pyを廃止し、各handlerが共通メソッドと契約の検証入口を直接呼ぶ。
- 補完の可視性変更には、個別に構築済みのレコードを渡す。本文不正・未着手のレコードから待機指示は生成せず、receiptHandleは従来どおり操作時に検証する。

Invariants: 全IDの事前検証、本文・JSON・イベント不正の個別再配信、入力順の逐次実行、キャンセル伝播、工程別診断と通知、補完の残り時間と可視性変更を維持する。

Non-goals: イベント型・Consumer・送信契約・DB・依存・AWS設定・共通バッチ実行基盤の変更。

Done: 5工程から重複JSON解析を除去し、本文型の検証はsqs/test_records.py、JSON解析はsqs/test_json.py、契約接続と配送制御は既存の工程別テストで確認する。異なる拒否条件は別のテストで記述する。

検証結果（2026-09-23）: app全体と変更PythonファイルのRuff lint・format、単体7,293件、専用ComposeでのDB統合1,302件、対象ローカル76件（backfill・outbox_relay・completion）が成功した。単体は`pytest tests/ -m 'not integration'`、DB統合は`make test-integration TEST_COMPOSE_PROJECT=vector-test-sqs-json-20260923`で分離した。差分チェックと専用Composeのコンテナー・ネットワーク・ボリュームの残存なしを確認した。ローカル全体・AWS実接続・デプロイは未実施。
