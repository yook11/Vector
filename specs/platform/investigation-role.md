# 本番データ調査のDBロール

## 作業定義

- Problem: 本番DBを人が調べるとき、読み取りだけに限ったロールが無い。調査にmasterや所有者vectorを使うと、操作ミスがそのまま本番データの変更・削除や、authの秘密値の表示になる。
- Evidence: `backend/db_roles.json`とdb-provision.sqlのロール一覧、n3によるvector_appのDEFAULT PRIVILEGES、[DBロール作成の運用経路](database-role-management.md)、DB踏み台（`infra/aws/platform_bastion.tf`と`infra/aws/README.md`の「DB 踏み台」節）、RDSのIAM DB認証。
- Invariants:
  - 既存ロールの権限を変更しない。
  - 書き込み・スキーマ変更・authの参照を持たない。
  - DB IAM認証とTLS（verify-full）を維持し、passwordを持たない。
  - 踏み台の境界（admin専用、平常時は存在しない）を変更しない。
  - 権限の正本は初期化SQLとmigrationとし、テストのfixtureでGRANTを足さない。
- Non-goals: 調査専用のIAM・SSO権限、踏み台の常設、ロールの既定値（ALTER ROLE ... SET）の管理、行・列単位の制限、ロール作成の入力形式の拡張。
- Done: vector_investigationに許可一覧どおりの権限を付与し、ローカル実DBで照合する。本番で踏み台越しにIAM認証で接続し、publicの表を読めて、書き込みとauthの参照が拒否されることを確認する。

## 位置づけ

操作ミスを防ぐためのロールで、アクセス制御の境界ではない。接続するのは踏み台を使えるadminで、adminは所有者を含む他のロールの認証トークンも発行できる。

## 権限の粒度

- 読み取り: publicの全テーブル（view・materialized viewを含む）を表単位でSELECTできる。調べる対象を事前に限定できないため。agentの会話履歴など利用者の入力も含むが、調査で見る前提とする。
- 新しい表: vectorがpublicに作る表へSELECTを自動で付与する（DEFAULT PRIVILEGES）。表を追加するたびにmigrationを要さないため。
- auth: 何も付与しない。sessionのトークン、accountのパスワードハッシュとOAuthトークンを含むため。
- 書き込み・TRUNCATE・REFERENCES・sequence・GRANT OPTIONは付与しない。
- 接続: 接続先DBのCONNECTとpublicのUSAGEを直接付与する。
- 実行ロールと同じく、superuser・DB作成・ロール作成・RLS迂回・replicationを持たず、他のロールにSET ROLEできない。

## セッションの制限

重いクエリと開いたままのトランザクションは、アプリと共用のRDSに負荷をかけ、migrationのロック待ちを通じてアプリを止めうる。接続時のセッション設定でstatement_timeoutとidle_in_transaction_session_timeoutを指定する。ロールの既定値にはしない。ロール作成の経路はロールの属性を扱わず、所有者vectorは他のロールの設定を変更できないため。書き込みはGRANTで拒否されるため、default_transaction_read_onlyは使わない。具体的な接続手順はprivate runbookに置く。

## ロール作成と付与

ロールは`backend/db_roles.json`に加え、既存のDBロール作成経路で作成する。新規環境の初期構築SQL、ローカルの初期化script、CIのロール作成、テスト用composeの接続値にも同名ロールを加え、ローカルとCIではNOLOGINで作成する。ロール作成の設定変更はcontractのmigrationと同じ変更に含められないため、付与より先に反映する。

付与は新しいAlembic revisionで行い、ロールが無ければ停止する。MIGRATION_KINDはcontractとし、lock_timeoutとstatement_timeoutは各5秒とする。downgradeは付与した権限とDEFAULT PRIVILEGESだけを取り消し、ロールは残す。

## 接続

アプリはこのロールで接続しないため、本体Terraformのdb_usersと接続URLには加えない。接続許可（rds-db:connect）はadminの権限で足り、IAMの変更は要らない。踏み台のport forward越しに、IAMの認証トークンとverify-fullで接続する。

## 検証

- 許可一覧: `local_tests/permissions/test_investigation_permissions.py`で、public・authの表・列・sequenceの権限が許可一覧と一致することを確認する。
- 共通の境界: `test_role_boundaries.py`に新ロールを加える。
- migration: `local_tests/migrations/`でupgrade・downgradeの往復、ロール不在時の停止、既存データとACLの維持、migration適用後にvectorが作った表を読めることを確認する。
- 本番: 付与の適用後、踏み台越しに接続し、publicの表のSELECTが通り、INSERTとauthの表のSELECTが拒否されることを確認する。
