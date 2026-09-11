# システムテスト共通DB基盤

Status: ローカル共通基盤の実装・検証完了（2026-09-11）

## Problem

モデルの`create_all()`で作る通常の実DBテストとは独立して、既存の初期化処理と全migrationで構築したDBをアプリ用ロールから検証する。

## Evidence

- `docker-compose.test.yml`と`infra/db/init/01_create_app_users.sh`: 一時Postgres・ロール作成。
- `frontend/src/lib/auth/auth.cli.ts`: Better Auth管理のスキーマ。
- `backend/alembic/`: アプリのスキーマ・権限・初期データ。
- `backend/app/db/engine.py`と`backend/tests/iam_fixtures.py`: 製品Engineとローカル署名器。

## Invariants

- 共通基盤を`backend/local_tests/`に置き、工程別テストはそのfixtureを利用する。
- テスト一式（並列実行時はworker）ごとに専用Composeプロジェクトを生成する。
- 初期化・Better Auth・Alembic適用は一式につき1回で、`stamp`や`create_all`を使わない。
- 各ケースは適用済みDBのコピーを使用し、初期データ・制約・テーブル権限を保持する。
- コピー元への接続を閉じてから複製する。DBレベルの独自ACLは複製されないため、その設定を検出した場合は試験を停止する。
- 管理接続は構築・データ準備・削除に限定し、権限試験は`vector_app`で実行する。
- `.env`を使わず、接続先と認証情報は専用Composeから取得する。
- 初期化失敗・テスト失敗でも専用環境を削除し、必須試験のskipは合格にしない。

## Non-goals

製品コード・既存migration・権限の変更、通常の実DBテストの移行、AWSアクセス、AI呼び出し、同一アプリイメージの検証は含めない。

## Done

- 空DBから対象コードのAlembic headへ到達し、Better Authテーブルも存在する。
- アプリロールで記事を読み、ベクトル更新・監査追加をcommitし、別接続で結果を確認する。
- 禁止されたDDL・認証テーブルへのアクセスが権限エラーになる。
- ケース間でcommit済みデータとsequenceの変更を持ち越さない。
- 一式を2回実行して構築・検証・削除が成功する。
- `check`の該当検証を実行し、結果を記録する。

複製方式は[PostgreSQLのtemplate database](https://www.postgresql.org/docs/current/manage-ag-templatedbs.html)に従う。常設DBの使い回しではなく、実行内で構築結果を再利用する。

## Implementation

`make test-local`で`backend/local_tests/`を実行する。`database.py`が初期化・検証・複製・削除を担当し、`conftest.py`の`system_database`を工程ごとに共有する。利用方法は同ディレクトリのREADMEに記載した。

Better Authには正本のCLI設定と関連ソースを一時ディレクトリへコピーして渡す。CLI版は既存frontend Dockerfileの宣言を使い、schemaの手書き代替や製品設定の変更は行わない。

## Verification

- Ruff lint・format checkと`git diff --check`が成功。
- 単体テスト6,581件成功（共通基盤の初期化／テスト失敗時の後片付けを含む）。
- `make test-system`を空環境から2回実行し、それぞれ7件成功・skipなし。CLIキャッシュのある環境で11.13秒・12.27秒（構築・migration・検証・削除を含む）。
- 記事の読み取り、行ロック、ベクトル更新、監査追加を製品Engineと`vector_app`で実行し、commit後に別接続から保存を確認した。
- 禁止操作の拒否、commit済みデータ・sequenceのケース間分離、失敗ケースの未解放接続の回収を確認した。
- 通常の`make test-integration`は再実行で1,402件成功・22件skip。skipは従来環境にAlembic適用済みschemaがない既存の権限試験であり、新しいDB契約・基盤テストの結果には含めない。
- 初回の通常実DBテストでは既存の`test_timeout_covers_business_work_but_not_failure_handling[ai]`が1件失敗した。50ms期限がAI到達前のDB取得中に切れた場合と整合する結果で、単独2ケースと全体の再実行は成功した。この時間依存の試験は変更しておらず、揺らぎが解消したとは扱わない。
- 製品コード・既存migration・権限・frontendソースの差分はない。AWS・同一アプリイメージの試験は未実施。

## テスト配置の整理（2026-09-11）

- Problem: 分類フォルダを増やした結果、実DBが必要な検証と準備条件の区別が読みにくくなっていた。
- Evidence: Alembic headは既に準備処理で確認しており、テーブル・初期データの存在確認も準備条件である。権限とケース分離は実DBが必要だが、URL組み立てと準備処理の単体テストは実DBを使わない。
- Invariants: 製品の権限・migration・ケース分離の方法を維持し、既存の確認事項を削除しない。
- Non-goals: 業務システムテストの追加、製品コード・schema・認証の変更、既存の通常結合テストの移行。
- Done: `local_tests/database.py`と`conftest.py`を共通準備とし、`test_database_permissions.py`に権限5件、`test_database_isolation.py`に実DBの分離・回収2件をまとめる。DB不要の単体5件は`tests/test_local_database.py`に置く。移動後の参照と実行を確認する。

マイグレーション到達と必要なAuthテーブル・カテゴリ初期データの存在は、DB準備時に確認して不成立なら停止する。
ローカル一式の実行名は`make test-local`とする。上記Verificationの`make test-system`は改名前の実績であり、業務フロー7件の成功を意味しない。
今後の業務システムテストは`local_tests/<工程名>/`に追加する。

整理後の検証: Ruff lint・formatと差分チェックが成功。`make test-local`は7件成功・skipなし、単体テストは6,581件成功。既存DB結合テストは専用Composeプロジェクトで1,402件成功・既存22件skip、終了後の環境削除も成功した。
