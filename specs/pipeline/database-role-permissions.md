# ローカル実DBのロール権限

## 作業定義

- Problem: 既存20件は通常の結合テスト環境でskipされ、禁止操作の抜けや実行内容と名前の不一致がある。
- Evidence: n3・y2・z14・z22の権限migration、既存ロールテスト、共通のmigration適用済みDBを照合した。
- Invariants: 製品権限とmigrationを変更しない。実ロールで接続する。期待値はmigrationから自動生成せず、下記の許可仕様として定義する。各ケースのDBは分離する。
- Non-goals: ユーザー作成からログインまでの実装、AWS IAMの実認証、既存データの移行、全PostgreSQL機能のアクセス制御検証。
- Done: 許可一覧との一致と代表的な実操作をskipなしで検証し、旧ロールテストを置換する。既存Auth構造契約2件は動作テストへの置換まで維持する。

## 許可一覧

DMLはSELECT・INSERT・UPDATE・DELETEを表す。

| ロール | 対象 | 許可 |
|---|---|---|
| vector_auth | auth内の全テーブル | DML |
| vector_app | public内の全テーブル | DML |
| vector_app | auth.user | SELECT・REFERENCES |
| vector_collect | public.news_sources | SELECT |
| vector_collect | public.analyzable_articles | SELECT・INSERT |
| vector_collect | public.incomplete_articles | DML |
| vector_collect | public.pipeline_events | INSERT、id・occurred_atのSELECT |
| vector_collect | public.outbox_events | INSERT、event_id・schema_version・occurred_at・next_attempt_at・attempt_countのSELECT |

対象はpublic・authの通常表、partitioned table、view、materialized view、foreign tableとその列。
テーブル操作はDML・TRUNCATE・REFERENCES・TRIGGER・MAINTAIN、列操作はSELECT・INSERT・UPDATE・REFERENCESを照合する。
許可一覧にない操作は禁止し、権限の再付与（GRANT OPTION）も禁止する。
明示したCollectのテーブル・列が存在することも確認し、存在しない対象が収集から消えて合格することを防ぐ。
新しいテーブルも実DBのカタログから収集するため、Auth/Appは担当schemaのDMLが必要で、Collectは未列挙なら禁止となる。
将来オブジェクトを生成するDEFAULT PRIVILEGESそのものの試験ではなく、対象コードの全migration適用後の権限を検証する。

## 採番と管理権限

- Auth: auth内のsequenceにUSAGEのみ。
- App: public内のsequenceにUSAGE。y2で既に付与したSELECT・UPDATEは、次の所有テーブルに限定して保持する。
  - agent_message_sources、analyzable_articles、analyzed_articles、article_curations、curation_noises、incomplete_articles、categories、news_sources、out_of_scope_articles、pipeline_events、query_embedding_cache、weekly_briefings。
- Collect: analyzable_articles・incomplete_articles・pipeline_eventsに所有されるsequenceにUSAGEのみ。
- 上記以外のsequence権限とGRANT OPTIONは禁止する。
- 3ロールはsuperuser・DB作成・ロール作成・RLS迂回・replicationを持たず、管理ロールvectorや他の実行ロールにSET ROLEできない。
- public・auth内のCREATEは禁止し、許可操作のために必要なschema USAGEを確認する。

Appのsequence追加権限は現行契約の明示であり、最小権限の再設計は今回行わない。
RLSによる行単位の可視性、関数のEXECUTE、全組み込みロールへの切替、システムschema、DB単位のTEMP等は本試験の保証範囲外。

## 実操作

- App: 自分で追加したカテゴリを別接続で読み、更新・削除が確定したことを確認する。
- Auth/App: auth.userのSELECT。Auth: rateLimitのDELETE文の実行権限。
- Collect: 記事・監査・OutboxのINSERTと採番・RETURNING・commit後の保存確認。未完成記事のCRUD。
- 禁止: 別schemaへのアクセス、AuthへのApp書き込み、news_sourcesへのCollect書き込み、記事の更新・削除、監査・Outboxの本文参照・更新・削除・TRUNCATEなど。

禁止操作はInsufficientPrivilegeErrorだけを成功とする。WHERE falseの操作は権限拒否の確認であり、行の変更結果やRLSの保証とはしない。
権限照会には[PostgreSQL 18の実効権限関数](https://www.postgresql.org/docs/18/functions-info.html#FUNCTIONS-INFO-ACCESS-TABLE)を使用し、列単位の付与や継承された権限も確認する。

## 配置と実行

- `backend/local_tests/test_database_permissions.py`: 許可一覧・権限照合・実操作40件。
- `backend/local_tests/test_auth_provisioning_schema.py`: 旧テストから保持したAuth構造契約2件。
- 旧`backend/tests/test_db_user_isolation.py`と独自の接続・skip処理は削除する。
- Outbox個別migrationの往復検証は既存の`backend/tests/outbox/test_collect_permissions_migration.py`に残す。
- 実行: backendで`uv run pytest local_tests/test_database_permissions.py local_tests/test_auth_provisioning_schema.py -q`。

## 検証結果（2026-09-11）

- 権限40件と保持したAuth契約2件は42件成功・skipなし。
- ローカル一式は46件成功・skipなし（別作業で追加されたEmbedding2件とDB分離2件を含む）。
- 単体6,581件、通常DB結合1,400件が成功し、旧ロールテスト由来の22件skipは解消した。
- 一時DBでUPDATE権限を剥奪、未許可テーブルのSELECTを付与、未許可列payloadのSELECTを付与する3つの変更をそれぞれ検出した。各変更はケースDBの削除で回収し、恒久的な権限変更や検証用テストの追加はしていない。
- Ruff lint・formatと差分チェックが成功し、専用Compose環境は終了時に削除した。
- 初回の照合では、旧sequence名keyword_categories_id_seqから所有テーブルを誤ってkeyword_categoriesと定義したため失敗した。c1のテーブル改名を確認し、許可一覧を実際の所有テーブルcategoriesに訂正した。
