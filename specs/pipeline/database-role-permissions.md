# ローカル実DBのロール権限

## 作業定義

- Problem: 各実行ロールの必要権限と禁止操作を明示し、Relayの配信処理を限定した権限で成立させる。
- Evidence: n3・y2・z14・z22・z23の権限migration、既存ロールテスト、共通のmigration適用済みDBを照合した。
- Invariants: Auth・App・Collectの既存権限を維持する。実ロールで接続する。期待値はmigrationから自動生成せず、下記の許可仕様として定義する。各ケースのDBは分離する。
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
| vector_outbox_relay | public.outbox_events | 下記11列のSELECTと7列のUPDATE |
| vector_auth_rate_limit_cleanup | auth.rateLimit | lastRequest列のSELECTと表のDELETE |
| vector_article_analysis | public.analyzable_articles・categories | SELECT |
| vector_article_analysis | public.article_curations・curation_noises・out_of_scope_articles | SELECT・INSERT |
| vector_article_analysis | public.analyzed_articles | SELECT・INSERT、embedding列のUPDATE |
| vector_article_analysis | public.pipeline_events | INSERT、id・occurred_atのSELECT |
| vector_article_analysis | public.outbox_events | INSERT、event_id・schema_version・occurred_at・next_attempt_at・attempt_countのSELECT |
| vector_backfill | public.analyzable_articles | SELECT・DELETE、id列のUPDATE |
| vector_backfill | public.article_curations・analyzed_articles | SELECT、id列のUPDATE |
| vector_backfill | public.curation_noises・out_of_scope_articles・news_sources | SELECT |
| vector_backfill | public.assessment_backfill_exclusions・embedding_backfill_exclusions | SELECT・INSERT |
| vector_backfill | public.incomplete_articles | SELECT、status・leased_until・updated_at列のUPDATE |
| vector_backfill | public.pipeline_events | INSERT、id・occurred_atのSELECT |
| vector_api | public.analyzable_articles・article_curations・analyzed_articles・categories・weekly_briefings・trends_snapshots・incomplete_articles・curation_noises・out_of_scope_articles・assessment_backfill_exclusions・embedding_backfill_exclusions・agent_message_sources | SELECT |
| vector_api | public.news_sources | SELECT・INSERT・DELETE、is_active・updated_at列のUPDATE |
| vector_api | public.watchlist_entries | SELECT・INSERT・DELETE |
| vector_api | public.agent_threads | SELECT・INSERT・DELETE、updated_at列のUPDATE |
| vector_api | public.agent_messages | SELECT・INSERT |
| vector_api | public.agent_runs | SELECT・INSERT、status・error_code列のUPDATE |
| vector_api | public.agent_user_daily_quotas | SELECT・INSERT、used_count列のUPDATE |
| vector_api | public.pipeline_events | stage・event_type・outcome_code・source_id・occurred_atのSELECT |

RelayのSELECT列はevent_id・event_type・schema_version・payload・occurred_at・published_at・next_attempt_at・attempt_count・lease_token・leased_until・delivery_stopped_at。
UPDATE列はlease_token・leased_until・attempt_count・published_at・next_attempt_at・delivery_stopped_at・delivery_stop_reason。
delivery_stop_reasonは更新だけを許可する。表全体へのSELECT／UPDATEやINSERTは付与せず、イベント本文の変更とイベント作成・削除を禁止する。

対象はpublic・authの通常表、partitioned table、view、materialized view、foreign tableとその列。
テーブル操作はDML・TRUNCATE・REFERENCES・TRIGGER・MAINTAIN、列操作はSELECT・INSERT・UPDATE・REFERENCESを照合する。
許可一覧にない操作は禁止し、権限の再付与（GRANT OPTION）も禁止する。
明示したCollect・Relay・記事分析・backfill・APIのテーブル・列が存在することも確認し、存在しない対象が収集から消えて合格することを防ぐ。
新しいテーブルも実DBのカタログから収集するため、Auth/Appは担当schemaのDMLが必要で、Collect・Relay・記事分析・backfill・APIは未列挙なら禁止となる。
将来オブジェクトを生成するDEFAULT PRIVILEGESそのものの試験ではなく、対象コードの全migration適用後の権限を検証する。

## 採番と管理権限

- Auth: auth内のsequenceにUSAGEのみ。
- App: public内のsequenceにUSAGE。y2で既に付与したSELECT・UPDATEは、次の所有テーブルに限定して保持する。
  - agent_message_sources、analyzable_articles、analyzed_articles、article_curations、curation_noises、incomplete_articles、categories、news_sources、out_of_scope_articles、pipeline_events、query_embedding_cache、weekly_briefings。
- Collect: analyzable_articles・incomplete_articles・pipeline_eventsに所有されるsequenceにUSAGEのみ。
- Relay: sequence権限なし。publicのUSAGEを付与し、DB・schema・tableの所有者にならない。
- 記事分析: article_curations・curation_noises・analyzed_articles・out_of_scope_articles・pipeline_eventsに所有されるsequenceにUSAGEのみ。
- backfill: pipeline_eventsに所有されるsequenceにUSAGEのみ。
- API: news_sourcesに所有されるsequenceにUSAGEのみ。
- 上記以外のsequence権限とGRANT OPTIONは禁止する。
- 各実行ロールはsuperuser・DB作成・ロール作成・RLS迂回・replicationを持たず、管理ロールvectorや他の実行ロールにSET ROLEできない。
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

- `backend/local_tests/permissions/`: 許可一覧・権限照合・代表的な実操作。Relayの禁止操作はSQLSTATE 42501で拒否を確認する。
- `permissions/test_auth_permissions.py`・`test_app_permissions.py`・`test_collect_permissions.py`・`test_outbox_relay_permissions.py`・`test_article_analysis_permissions.py`・`test_backfill_permissions.py`にロールごとの期待値と操作を置き（backfillは期待値だけを置き、成功する操作は`local_tests/backfill/`のフロー試験が確かめる）、`test_role_boundaries.py`に接続主体・管理属性・ロール切替・schema権限の共通検査を置く。
- `permissions/support.py`は実効権限と対象オブジェクトを取得し、期待する許可一覧の判定は各ロールのテストが担う。
- `backend/local_tests/outbox_relay/`: 既存Relayテストの実行接続をvector_outbox_relayへ切り替え、配送成功・再試行・停止・並行実行・障害時の保存結果を検証する。repository操作ごとの成功権限テストは重ねず、Relayの振る舞いで確認する。
- `backend/local_tests/migrations/test_outbox_relay_migration.py`: z22からz23への往復、既存Outboxデータと既存ACLの維持を確認する。
- `backend/local_tests/test_auth_provisioning_schema.py`: 旧テストから保持したAuth構造契約2件。
- 旧`backend/tests/test_db_user_isolation.py`と独自の接続・skip処理は削除する。
- Outbox個別migrationの往復検証は既存の`backend/tests/outbox/test_collect_permissions_migration.py`に残す。
- 実行: リポジトリルートで`make test-local`。

## Relayロールの初期化

ロール作成は共通init scriptとDB初期構築SQL、表・列のGRANTはz23のAlembic migrationが担当する。ロール未作成の場合、migrationはエラーで停止する。

共通ローカル初期化はNOLOGINで作成し、local_testsのセッション初期化でテスト用LOGINと固定パスワードを設定する。業務GRANTはfixtureへ追加しない。CIのhead適用前にも同名ロールを作成する。RDSの初期構築SQLはLOGINとrds_iamを付与する。

既存開発DBには、管理用psql接続で`CREATE ROLE vector_outbox_relay NOLOGIN;`を一度実行してからmigrationを適用する。既に同名ロールがある場合は再作成しない。downgradeはz23が追加した列GRANTとpublicへの直接USAGEを撤去し、ロール自体は保持する。

## 検証結果（2026-09-11）

- 権限40件と保持したAuth契約2件は42件成功・skipなし。
- ローカル一式は46件成功・skipなし（別作業で追加されたEmbedding2件とDB分離2件を含む）。
- 単体6,581件、通常DB結合1,400件が成功し、旧ロールテスト由来の22件skipは解消した。
- 一時DBでUPDATE権限を剥奪、未許可テーブルのSELECTを付与、未許可列payloadのSELECTを付与する3つの変更をそれぞれ検出した。各変更はケースDBの削除で回収し、恒久的な権限変更や検証用テストの追加はしていない。
- Ruff lint・formatと差分チェックが成功し、専用Compose環境は終了時に削除した。
- 初回の照合では、旧sequence名keyword_categories_id_seqから所有テーブルを誤ってkeyword_categoriesと定義したため失敗した。c1のテーブル改名を確認し、許可一覧を実際の所有テーブルcategoriesに訂正した。

## 認証カウンター掃除ロールの追加・適用

`vector_auth_rate_limit_cleanup`は認証カウンター掃除Lambda用の専用ロール。
接続先DBへのCONNECT、auth schemaへのUSAGE、`auth."rateLimit"."lastRequest"`への
列SELECT、同テーブルへのDELETEだけを直接付与する。key・countの参照、INSERT・
UPDATE・TRUNCATE・CREATE、他の認証テーブルやpipeline_eventsへの権限は追加しない。
PUBLIC由来の接続権限は変更しない。10分超の行だけを削除する条件は後続アプリの
DELETE句で保証し、DBのDELETE権限自体は行の年齢を制限しない。

ロール作成は`backend/db_roles.json`を入力とするAWS DB roles workflow、GRANTは
`z24_auth_cleanup_grants`のAlembic migrationが担当する。migrationはロール・表・
lastRequest列が不足すれば停止し、Better Authのschemaを作成・変更しない。

本番適用は次の順とする。

1. PRをマージし、AWS DB rolesを最新mainで起動して`production-db-roles`を承認する。
2. 作成成功後、同じ対象commitを指定してAWS DB migrationを`contract`モードで起動する。
3. 承認前にmigration範囲が意図した権限追加であることを確認し、適用後にrevisionと
   対象ロールの権限を読み取り確認する。本番カウンターを削除する試験は行わない。
4. Lambda・Scheduler・Lambda側のrds-db:connectは別PRで追加する。

本PRでは旧Taskiqの掃除を継続し、認証機能・既存データ・監査履歴の掃除は変更しない。
downgradeは本migrationの直接GRANTのみを取り消し、ロール自体は保持する。
後続Lambdaの稼働開始後に戻す場合は、先にその定期起動と実行を停止する。

新規ローカルDBは共通init scriptがNOLOGINロールを作成する。既存開発DBでは
管理接続で`CREATE ROLE vector_auth_rate_limit_cleanup NOLOGIN;`を一度実行し、
Better Authのmigration後にAlembicを適用する。local_testsでは共通基盤がテスト用
LOGINだけを設定し、GRANTは製品migrationに委ねる。
