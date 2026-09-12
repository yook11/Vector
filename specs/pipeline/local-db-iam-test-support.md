# ローカル実DBテストのIAM接続支援

Status: 共通ヘルパーの実装・ローカル検証完了（2026-09-11）

## Problem

AWS向けEngineをローカルの実Postgresで検証するため、トークン生成をテストDBの認証情報へ差し替える仕組みを共有する。Embedding・Assessmentでは既存ヘルパーを使っているが、Relayの実DBテストはIAMを無効にしている。

共有するのはトークン値や接続ではなく、テスト専用の署名器の差し替えである。

## Evidence

- `backend/app/db/iam.py`: 共通の`build_iam_password_provider`とSDK clientの取得口。
- `backend/app/db/engine.py`: 接続時のpassword providerと各用途のEngine設定。
- `backend/app/lambda_handlers/article_analysis_lifecycle.py`: 呼び出しごとのSDK clientから署名器を渡す経路。
- `backend/tests/lambda_handlers/iam_fixtures.py`: 移動前の共通ヘルパー。
- `backend/tests/lambda_handlers/test_handler_integration.py`: IAMを無効にしていたRelayの実DBテスト。

## Invariants

- 製品のSettings・Engine・Session・IAM必須条件は変更しない。
- `build_iam_password_provider`を実行し、SDKの署名器だけがテストDBのパスワードを返す。
- 設定へ渡すURLからパスワードを除き、host・port・user・database・queryを維持する。
- 署名対象のhost・port・userがテストDB接続先と一致しない場合は失敗する。
- テストごと、pytest workerごとの`test_database_url`を使い、認証情報をグローバルに保持しない。
- 差し替えは明示的に呼び出したテストの範囲に限定し、終了後に元へ戻す。
- DB接続・SQL実行・commit・rollback・再接続・接続解放は実物で検証する。

## 共通ヘルパー

配置先は`backend/tests/iam_fixtures.py`、名前は既存の`inject_test_db_signer`を維持する。

```python
from app.lambda_handlers import article_analysis_lifecycle

database_url = inject_test_db_signer(
    monkeypatch,
    test_database_url,
    resources_module=article_analysis_lifecycle,
)
```

`resources_module`を指定すると、Consumerの呼び出し単位のSDK clientを差し替える。省略すると、Relay・API・workerなどが使う`app.db.iam`の共通SDK client取得口を差し替える。いずれも同じ署名器の契約を使う。

既存の対象はEmbedding・Assessmentの資源／Consumer実DBテストとRelayの入口実DBテスト。通常のパスワード認証や署名アルゴリズム自体の単体テストには一律適用しない。

## Non-goals

- 本番コード、DB schema、認証・認可設定、dependencyの変更。
- RDS IAM認証・TLS・AWS credentialの取得／更新が実環境で成立することの保証。
- 全DBテストのIAM化、全Engineの新規テスト追加。
- Alembicでのシステムテスト環境構築、同一イメージでの実行、AWSスモークテスト。

## Done

- 既存のConsumerとRelayの対象テストが同じヘルパーを使う。
- 認証情報の差し替え後も、保存・障害伝播・再接続・接続解放の既存実DBテストが成功する。
- 接続先不一致を隠さず、URLの接続情報保持とテスト終了後の復元を検証する。
- lint・format・単体テストと`make test-integration`の結果を記録する。

このステップの合格はIAM経路のローカル接続支援の完成を意味する。Embedding全体のシステムテストやAWS検証の完了とは区別する。

## Verification

- 製品コードの差分はなく、既存のConsumerとRelayの実DBテストを共通ヘルパーへ接続した。
- Ruff lint・format checkが成功した。
- `uv run pytest tests/ -m unit -x -q`: 6,576件成功。
- 共通ヘルパーの最終状態の個別検証: 11件成功。
- `make test-integration TEST_COMPOSE_PROJECT=vector-test-iam-support-20260911-5n4tq PYTEST_ARGS='-x -q -rs'`: 1,402件成功・22件skip、終了時に一時コンテナとネットワークを削除した。
- skipは既存のDBロール分離テストで、Alembic適用済み`public.watchlist_entries`が一時DBにないため。DBロール分離の検証成功には数えない。
- 同一イメージでのシステムテストとAWS実環境での検証は未実施。
