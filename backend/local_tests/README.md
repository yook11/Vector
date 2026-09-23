# migration適用済みDBを使うローカルテスト

実際のDB権限・トランザクション・保存結果を確認するテストを置く。
DB不要の単体テストは`backend/tests/`に置く。

## 実行

Docker、Node/npm、backendの`uv sync --frozen`、frontendの`npm ci`が必要。
Better Auth CLIはfrontend developmentイメージと同じ版をnpxで起動する。

```sh
# リポジトリルートで全件実行
make test-local

# backendで対象を絞って実行
uv run pytest local_tests/permissions/ -x -q
uv run pytest local_tests/outbox_relay/ -x -q
uv run pytest local_tests/migrations/ -x -q

# ファイル追加・移動後は全体の収集も確認
uv run pytest local_tests/ --collect-only -q
```

## 配置

- `permissions/`: ロール別の許可一覧・禁止操作と、共通の権限境界。
- `migrations/`: migrationごとのupgrade・downgrade、既存データや権限の維持。
- `outbox_relay/`、`acquisition/`、`curation/`、`assessment/`、`embedding/`、`completion/`、`backfill/`: 各処理の動作。
- 直下の`test_*.py`: DB分離や共通ライフサイクルなど、工程をまたぐ基盤。

共通の接続設定は`conftest.py`、データ準備・観測処理は`support.py`に置く。
同名のテストファイルを別ディレクトリへ置く場合は、`__init__.py`でpackageとして区別する。

## DBの使い方

`system_database_template`が専用Postgres・ロール・Better Auth・Alembic headを準備する。
`system_database`はケースごとにDBを複製し、終了後に削除する。
TEMPLATEが引き継がないDB単位のACLは、migration適用済みDBの実際のACLから復元する。

- `system_database.connect("vector_app")`: 指定ロール自身で認証する接続。
- `system_database.url("vector_app", sqlalchemy=True)`: 製品Engine用の接続URL。
- `system_database.connect("vector")`: データ準備・結果観測用の管理接続。

処理の実行には対象のアプリロールを使う。権限の正本は初期化SQLとmigrationとし、fixtureでGRANTを足さない。
各ケースでクラスタ共通のロール定義を変更せず、複製元DBにも接続しない。
`tests/conftest.py`、`create_all`、全テーブルTRUNCATEは使わない。
接続値は専用Composeの定義から取得し、`.env`は使わない。

## テストを書くとき

- テスト名から条件と期待する振る舞いを読めるようにする。
- 準備・操作・検証を順に書き、重要な入力と期待値はテスト本文に残す。
- 異なる条件・期待結果は別ケースにする。parametrizeは操作と検証が同じ場合に使う。
- 共通化は接続設定・準備・観測を中心にし、フラグでシナリオを切り替えるhelperを作らない。
- 実DBで確認したい処理は実装を通し、外部通信は境界で差し替える。
- 複数対象を扱うのは混在バッチ・競合などの相互作用を確認する場合に限る。
- 並行処理は待機時間に頼らず進行条件を制御し、待機に上限を設けて`finally`で解放する。

READMEには実行・配置・共通ルールを残し、ケース一覧・件数・作業履歴は追記しない。
業務上の期待値はテストと仕様で確認する。
詳細は[共通DB仕様](../../specs/pipeline/system-test-database.md)と[ロール権限仕様](../../specs/pipeline/database-role-permissions.md)を参照。
