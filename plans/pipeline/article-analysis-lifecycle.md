# 記事単位AI分析のConsumerライフサイクル共通化

Status: 共通化の実装と、共通入口を直接検証するテスト配置への修正・検証が完了（2026-09-13）。2026-09-12に合意した境界・命名・型付けに沿って2工程を移行した。

## Problem

AssessmentとEmbeddingの`resources.py`・`composition.py`に、資源の準備、Consumerの構築、逆順の解放、初期化・終了障害の扱いが重複している。記事単位AI分析のLambda Consumerに限定して順序と所有期間を一度だけ定義する。

共通側が「いつAIクライアントを開閉するか」を決め、SDK側が「内部資源をどう生成・解放するか」を所有する。まずAssessmentとEmbeddingへ適用し、Curationはイベント駆動化時に同じ入口へ接続する。

## Evidence

- `backend/app/lambda_handlers/{assessment,embedding}/resources.py`: SSM取得、RDS署名器、Engine、session factory、Engine→RDSの解放が重複。
- `backend/app/lambda_handlers/{assessment,embedding}/composition.py`: resources→AIクライアント→Consumer、初期化段階の記録と逆順解放が重複。
- `backend/app/lambda_handlers/{assessment,embedding}/failure_recorder.py`: 両方が`record_initialization_failure(stage: str, error: Exception) -> None`と`record_cleanup_failure(resource: str, error: Exception) -> None`を実装済み。
- `backend/app/ai_providers/{deepseek,gemini}/client.py`: SDK・HTTPの生成・解放、通常の終了障害時の結果保持は各context managerが所有する。内部の終了手順は同一ではない。
- `backend/app/aws/ssm.py`: SSM通信資源は取得操作内で閉じ、値は`SecretStr`で返す。
- `backend/app/db/{engine,iam,session}.py`: 工程別Engine設定、IAM署名器、DB例外変換付きsession factoryを提供する。
- `specs/pipeline/{assessment,embedding}-consumer.md`: 呼び出し単位の所有、初期化・終了障害の扱い、IAM必須、AI待機中の接続返却の契約。
- `backend/tests/lambda_handlers/`、`backend/tests/analysis/embedding/test_consumer.py`、`backend/local_tests/{assessment,embedding}/`: 旧resourcesの直接利用・monkeypatchがあり、本体の移行に合わせた更新が必要。
- `backend/pyproject.toml`: Python 3.13以上。Ruff・pytestを導入済みで、静的型チェッカーの依存・設定はない。

## Invariants

1. 準備はAPIキー取得→RDS署名器→Engine・session factory→AIクライアント→Consumerの順。Consumerの貸し出し前にこれらを完了し、既存のSQS入力検証の位置を維持する。
2. 終了はAIクライアント→Engine→RDSの順。各記事で開いたセッションは処理側で閉じ、Engineのdispose前に返却する。Engine生成自体と実DB接続開始を混同しない。
3. 資源はLambda呼び出し内で共有し、呼び出し間では再利用しない。SSM取得も呼び出しごとに行う。
4. 資源取得直後に終了処理を登録し、後続の初期化失敗でも取得済み資源を解放する。
5. 通常の終了・診断障害で成功結果や元例外を上書きしない。キャンセル・プロセス終了は抑止しない。新しいtimeout・retry・shieldは追加しない。
6. 初期化例外の捕捉はConsumer構築まで。`yield`後の業務例外を初期化失敗として記録しない。
7. 工程別の診断名・資源名（engine、rds）を維持する。診断段階は共通側でresources・ai_client・consumerに固定し、呼び出し元から指定しない。診断へ秘密情報・接続URL・例外自由文を追加しない。
8. IAM認証・TLS・接続数・通信上限・DB例外変換・トランザクション境界を維持する。AI応答待ちにDB接続やロックを保持しない。
9. 共通側は具体的なSDK・工程別Consumer・設定クラス・Recorderをimportしない。準備済み依存はConsumerへ借用させる。

## Non-goals

- Curationのイベント駆動化、集約AI・Taskiq・Outbox relayへの適用。
- SQSバッチ処理、イベント検証、Consumerの業務処理・監査・再配信方針の共通化。
- SDK内部の生成・終了処理、DB Engine生成関数の統合、工程別設定・Recorderの変更。
- DB schema、認証・認可、API response、依存パッケージ、Terraform、AWS設定の変更・デプロイ。
- 共通resources DTO、互換ラッパー、工程レジストリ、汎用DI基盤の追加。
- テスト基盤全般の整理や、既存の無関係な`partial`の置き換え。

## 公開契約と型

新設先は`backend/app/lambda_handlers/article_analysis_lifecycle.py`、公開入口は`open_article_analysis_consumer`とする。必要な型は同じモジュール内に置き、型専用の階層を作らない。

- `ArticleAnalysisLifecycleRecorder(Protocol)`: 合意した2メソッドのみ。既存Recorderは構造的に適合するため継承追加・実装変更は不要。実行時の`isinstance`検査は追加しない。
- 型パラメーターは`ConsumerT`・`ClientT`。`open_client`が貸し出す型と`build_consumer`が受け取る型を`ClientT`で、構築結果と`yield`する型を`ConsumerT`で結ぶ。
- `SessionFactory`は`Callable[[], AbstractAsyncContextManager[AsyncSession]]`、`IamPasswordProvider`は`Callable[[], Awaitable[str]]`の型別名とする。既存のDB関数の実際の契約を維持する。
- 工程から渡す関数は型注釈付きの名前付き関数とし、`partial`を使用しない。
- 合意済みのキーワード引数を型として保持するため、生成関数の受け口は`__call__`だけを持つ小さなProtocolで表す。`EngineFactory`は`(*, password_provider: IamPasswordProvider) -> AsyncEngine`、`ClientFactory[ClientT]`は`(*, api_key: SecretStr) -> AbstractAsyncContextManager[ClientT]`、`ConsumerFactory[ConsumerT, ClientT]`は`(*, session_factory: SessionFactory, client: ClientT) -> ConsumerT`とする。

```python
@asynccontextmanager
async def open_article_analysis_consumer[ConsumerT, ClientT](
    *,
    aws_region: str,
    database_url: str,
    api_key_parameter_path: str,
    create_engine: EngineFactory,
    open_client: ClientFactory[ClientT],
    build_consumer: ConsumerFactory[ConsumerT, ClientT],
    failure_recorder: ArticleAnalysisLifecycleRecorder,
) -> AsyncIterator[ConsumerT]:
    ...
```

工程別の`open_assessment_consumer`／`open_embedding_consumer`は通常の`def`で共通context managerを返す。戻り型はそれぞれ`AbstractAsyncContextManager[AssessmentConsumer]`／`AbstractAsyncContextManager[EmbeddingConsumer]`とし、関数名・引数・`async with`での使い方は維持する。

設定を捕捉する名前付き関数は工程別入口の中に定義する。クライアント通信設定の組み立ても`open_client`の関数内で行い、実際の生成・設定失敗が共通側の初期化範囲に収まるようにする。共通context managerを返しただけではEngine・SDKを生成しない。

## 実装手順

### 1. 保証の配置を整理し、共通ライフサイクルを実装する

- 既存resources・compositionテストの保証を、共通の順序・失敗契約、工程別配線、実DBの接続管理に対応付ける。
- `backend/tests/lambda_handlers/test_article_analysis_lifecycle.py`へ共通契約のテストを配置し、上記モジュール・Protocol・型別名を実装する。
- `AsyncExitStack`でRDS終了、Engine終了、AI context managerを取得順に登録する。SSMの`asyncio.to_thread`と既存のSDK client作成設定を移す。
- `_close_rds`と`_dispose_engine`を共通側に一度だけ定義し、既存Recorder経由で通常の終了障害を記録する。初期化診断は`yield`の前までに限定する。
- 完了条件: 正常終了、各準備段階の失敗、通常の終了障害、業務例外、キャンセルで順序と結果保持を確認できる。

### 2. Assessment・Embeddingのcompositionを共通入口へ接続する

- 両`composition.py`で、設定を捕捉する`create_engine`・`open_client`・`build_consumer`の名前付き関数を用意し、共通入口へ渡す。
- 工程別のAPIキー取得先、Engine生成関数、SDK設定、モデル仕様、Consumer構築、Recorderを維持し、AIクライアントの診断段階は共通側のai_clientを使う。
- 両`resources.py`の責務とDTOを共通側へ吸収し、参照の移行後にファイルを削除する。
- handler本体・SDK側・DB生成関数・Recorder本体の振る舞いは変更しない。
- 完了条件: 2工程が同じ順序定義を使い、共通側に工程分岐がなく、利用側のConsumer型が保持される。

### 3. テストの参照と差し替え箇所を移行する

| 対象 | 移行内容 |
|---|---|
| `backend/tests/lambda_handlers/test_{assessment,embedding}_resources.py` | 共通の順序・終了・SSM取得保証を共通テストへ移す。残る工程別Settings・Engine設定のテストは保証を維持し、必要に応じ内容に合うファイル名へ整理する。 |
| `backend/tests/lambda_handlers/test_{assessment,embedding}_composition.py` | 工程別の設定・クライアント・Consumer配線と、共通入口へ遅延生成関数を渡すことを検証する。工程ごとの失敗診断名も維持する。 |
| `backend/tests/lambda_handlers/embedding_fixtures.py` | SQS処理を検証するfixtureはhandlerが借用するConsumerの入口で差し替え、削除されるresourcesに依存させない。 |
| `backend/tests/lambda_handlers/test_embedding_resources_db.py` | 共通入口を通してsession factoryを借用するテスト構成へ移す。製品APIにテスト用のresources公開口を増やさない。 |
| `backend/tests/analysis/embedding/test_consumer.py` | 呼び出し単位資源を使うテストを共通入口へ移し、実Consumerによる保存・切断後の再接続・失敗監査の保証を残す。 |
| `backend/local_tests/{assessment,embedding}/conftest.py` | SSM・RDS署名器の差し替え先を共通ライフサイクルへ移す。実SDK・Consumer・Repository・DBを通る経路を維持する。 |
| `backend/local_tests/{assessment,embedding}/test_invocation_resources.py` | Engine観測の差し替え先を各compositionが使う工程別Engine生成関数へ移す。接続数・返却・disposeの観測を維持する。 |

- DB接続をモックせず、既存`inject_test_db_signer`の`resources_module`へ新しい共通モジュールを渡す。共有ヘルパー自体の改名・再設計は不要。
- 旧resourcesをimportする箇所を再検索し、別名import・文字列参照・monkeypatchも含めて残存参照をなくす。
- 共通テストは正常時だけでなく、SSM／RDS／署名器／Engine／session factory／AI client／Consumer構築の途中失敗、SDK終了後のEngine・RDS解放、ログ障害、外部キャンセル・プロセス終了を対象とする。
- SSM取得中のキャンセル後も取得スレッド内でSSM clientが閉じられる既存の保証を維持する。強制終了や繰り返しキャンセル下での解放を新たに保証しない。
- 戻り型・生成関数に`Any`・`cast`で穴を開けない。テスト専用にConsumer・クライアントの共通基底クラスを作らない。
- 完了条件: 旧モジュール参照がなく、保証の移動先が説明でき、単体・実DB・ローカルシステムの各経路が新しい配線で動く。

### 4. 仕様更新と検証を完了する

- `specs/pipeline/assessment-consumer.md`・`specs/pipeline/embedding-consumer.md`の資源所有・組み立ての記述を共通入口へ更新する。既存の成功・失敗・設定契約は保持する。
- `backend/local_tests/README.md`やIAMテスト支援の説明は、参照先が変わる箇所だけ更新する。
- `/check`に従い、Ruffのlint・format、全単体、全DB統合、関連ローカルシステムテストを実施する。
- 完了条件: 下記Doneを満たし、未実行項目がある場合はその理由と未保証範囲を明示する。

## 検証手順

1. 変更した単体テストを実行し、共通ライフサイクルと2工程の配線を確認する。
2. backendで`uv run ruff check app/`・`uv run ruff format --check app/`を実行する。変更した`tests/`・`local_tests/`のファイルにも同じ検証を行う。
3. backendで`uv run pytest tests/ -m unit -x -q`を実行する。既存conftestが付与するマーカーでDB不要テストを明示選択する。
4. リポジトリルートで`make test-integration`を実行し、一時DB・Redisの終了まで確認する。
5. migration適用済みローカルDBで、backendから`uv run pytest local_tests/assessment/ local_tests/embedding/ -x -q`を実行する。外部AI・AWS通信だけを既存fixtureで差し替え、実handlerからの接続再利用・解放・失敗後回復を確認する。
6. `git diff --check`、旧resourcesへの残存参照、変更範囲を確認する。

型については、Protocol適合・引数名・キーワード専用引数・ClientTからConsumerTへの対応・工程別公開戻り型をコードで確認する。現在は静的型チェッカーが設定されていないため、Ruff・pytestの成功を静的型検証の成功とは報告しない。この変更で新しい型チェッカー依存は追加しない。

AWSへの実通信・デプロイ・AWS試験設備の作成は本計画の検証に含めない。ローカルシステムテストの前提環境がない場合も、製品コードのIAM分岐を変えたりテストをskipへ書き換えたりせず、未実行として明記する。

## Done

- 記事単位AI分析の準備・終了順序と通常の失敗時方針が`article_analysis_lifecycle.py`に一度だけ定義されている。
- Assessment・Embeddingが共通入口を使い、各compositionは設定・生成関数・Consumer構築・診断の配線だけを持つ。
- 各SDKが内部資源の生成・解放を引き続き所有し、具体的なクライアント型・Consumer型が型注釈でつながっている。
- 重複したresourcesモジュール・DTO・旧参照がなく、移行前の保証が適切なテスト層に残っている。
- 必要な検証が成功し、仕様と実コードが一致している。未実行の必須検証がある場合は検証完了としない。
- 実装開始時に作業ツリーを再確認し、現在進行中の無関係な変更や削除済みテストを巻き戻していない。


## 実装記録（2026-09-13）

- 共通ライフサイクル、Recorder・生成関数のProtocol、ConsumerT・ClientTを実装した。工程別compositionは型注釈付きの名前付き関数を渡す。
- 両resourcesモジュールとDTOを削除し、全コード参照・テストの差し替え箇所を移行した。SDK内部・handler本体・Engine生成関数・Recorder本体は変更していない。
- 工程別compositionテストは`test_article_analysis_composition.py`へ集約し、Assessment／Embeddingをパラメーターとして同じ配線契約を確認する。設定・Engine設定は`test_assessment_settings.py`／`test_embedding_settings.py`に残す。
- 共通資源の保証は`test_article_analysis_lifecycle.py`、実DBの保証は`test_article_analysis_lifecycle_db.py`へ配置した。実Consumerの保存・切断後再接続・監査テストと、2工程のローカルシステムテストを維持した。
- Ruff lint・format: app全体と変更した単体・統合・ローカルシステムテストが成功。
- 関連単体: `uv run pytest tests/lambda_handlers/ tests/test_iam_fixtures.py -m unit -x -q`で228件成功。
- 全単体: `uv run pytest tests/ -m unit -x -q`で6,641件成功。
- 全DB統合: `make test-integration PYTEST_ARGS='-x -q -rs'`で1,396件成功、skipなし。一時DB・Redisの終了・削除を確認した。
- ローカルシステム: `uv run pytest local_tests/assessment/ local_tests/embedding/ -x -q`で21件成功、skipなし。専用のmigration適用済みDBは終了後に削除された。
- `git diff --check`、新規ファイルの末尾改行・空白、旧resourcesへのコード参照がないことを確認した。
- 静的型チェッカーは既存環境にも導入されていないため未実行。型注釈・Protocol契約のコード確認に限定し、静的型検証済みとはしない。新規依存は追加していない。
- AWS実通信・デプロイは実施していない。


## テスト所有先の修正（2026-09-13）

- Problem: 共通化した資源管理を工程別ローカルテストから繰り返し確認しており、共有責務とテストの所有先が一致していなかった。
- Evidence: 共通`open_article_analysis_consumer`、工程別の旧`test_invocation_resources.py`、共通ライフサイクルの単体・DB部品テスト、各Consumerのセッション境界を照合した。
- Invariants: 共通入口を直接呼び、実Engine・実DBで準備順・所有期間・逆順解放・接続回収・呼び出し間の分離を確認する。SDK内部の終了処理はSDK側、記事処理内のセッション境界はConsumer側の保証とする。
- Non-goals: 工程別handlerを共通fixtureで切り替える構成、SDK内部の再実装、製品コード・設定・schema・権限の変更。
- Done: 共通資源のローカルテストを一箇所に置き、旧工程別資源テストと通常DB部品テストの重複を削除する。保存・応答・AI待機前の接続返却の保証は残し、全単体・全DB統合・全ローカルテストを通す。

| 保証 | 現在の所有先 |
|---|---|
| 共通の準備・所有・解放、実DB接続回収、再利用・ロールバック・タイムアウト・切断後回復 | `backend/local_tests/test_article_analysis_lifecycle.py`（共通入口を直接検証、工程別parametrizeなし） |
| 各ConsumerのAI待機前のセッション返却 | `backend/local_tests/{assessment,embedding}/test_session_boundaries.py` |
| 各工程の通信失敗・DB待機期限切れの保存と応答 | `backend/local_tests/{assessment,embedding}/test_event_processing.py` |
| DB接続前の初期化障害、終了・診断処理の二次障害、SSMスレッドのキャンセル | `backend/tests/lambda_handlers/test_article_analysis_lifecycle.py` |
| 工程別設定・SDK・Consumerの配線 | `backend/tests/lambda_handlers/test_article_analysis_composition.py` |
| SDK内部の閉じ方 | 既存のSDK別クライアントテスト |

旧工程別`test_invocation_resources.py`と通常DB部品の`test_article_analysis_lifecycle_db.py`は削除した。上記は先行実装のテスト配置と検証手順を置き換える。

### 修正後の検証結果

- Ruff lint・format: app全体、local_tests全体、変更した通常テストが成功。`git diff --check`も成功。
- 全単体: `uv run pytest tests/ -m unit -x -q`で6,644件成功。
- 全DB統合: `make test-integration PYTEST_ARGS='-x -q -rs'`で1,396件成功。
- 全ローカル: `uv run pytest local_tests/ -x -q`で80件成功。準備順の観測を追加した後、共通ライフサイクル11件を再実行して成功した。
- skipなし。既存の非推奨・Logfire警告は残る。テスト用DB・Redisの終了と一時環境の削除を確認した。
- 今回のテスト配置修正では製品コードを変更していない。
