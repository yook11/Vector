# InsightsのDBロール分離

## 作業定義

- Problem: Insights段（trend-discovery・briefingのworker）がvector_appで接続している。vector_appはpublic全表の読み書き削除、新しい表への自動付与、auth.userの参照を持つため、外部LLMの応答を保存するworkerの不具合や侵害が、記事・分析結果・利用者データ・監査の書き換えまで及ぶ。
- Evidence: 2つのworkerから到達する実SQL（トレンドとブリーフィングのrepository、監査）、CLIと上書き経路の撤去（#466）、n3・y2によるvector_appの付与、ECSのinsights段のdb_usersと接続URL、task系の共通権限境界。
- Invariants:
  - トレンド・ブリーフィングの生成結果、監査の記録、二重生成の防止（一意制約とON CONFLICT DO NOTHING）を変えない。
  - DB IAM認証とTLSを維持する。
  - vector_appの権限と、vector_appで動くagent workerは変更しない。
  - 権限の正本は初期化SQLとmigrationとし、テストのfixtureでGRANTを足さない。
- Non-goals: vector_appの縮小と廃止、agent workerのロール分離、APIが読むトレンド・ブリーフィングの変更、schedulerの接続URLの整理、ローカル開発Composeのbackendの接続ロールの切替。
- Done: vector_insightsに許可一覧どおりの権限を付与し、2つのworkerの全操作が新ロールで動くことをローカル実DBで確認する。ECSのinsights段が新ロールで接続し、insights段のタスクロールにvector_appへの接続許可が残らない。

## 権限の粒度

Insightsは公開済みの分析結果を読み、週次の成果物を追加するだけの処理で、既存行を書き換えない。

- 読み取り: analyzable_articles・article_curations・analyzed_articles・categoriesは表単位のSELECTとする。中身は公開ニュースで隠す対象がなく、列単位では読む列が増えるたびにGRANTが要る。
- 成果物: trends_snapshots・weekly_briefingsはSELECT・INSERTとする。生成済みかの確認とINSERTのRETURNINGに使う。二重生成は一意制約とON CONFLICT DO NOTHINGで防ぐ。
- 監査: pipeline_eventsはINSERTと、ORMがRETURNINGで受け取るid・occurred_atのSELECTだけとする。他の段の記録やpayloadは読めない。
- 更新・削除: 付与しない。既存行を上書きする経路は手動実行CLIだけにあり、CLIとともに撤去した（#466）。
- 採番: weekly_briefings・pipeline_eventsのidはsequenceで採番するため、そのsequenceのUSAGEだけを付与する。trends_snapshotsはwindow_endが主キーでsequenceを使わない。
- 接続: 接続先DBのCONNECTとpublicのUSAGEを直接付与する。authには何も付与しない。
- 新しい表への自動付与、REFERENCES、TRUNCATEは付与しない。外部キーの検証は親表の所有者権限で動く。新しく参照する表は明示的にGRANTする。読み取りを許可した表では列の追加・参照変更ごとに権限を分けず、クエリで必要な列だけ取得する。

生成済みかの判定と同時実行時の二重保存の防止は、GRANTではなくコードの判定と一意制約が担う。2つのworkerは同じロールで動くため、トレンドとブリーフィングの書き込みは区別しない。

## 許可一覧と操作

表・列・sequenceの許可は[ロール権限仕様](../pipeline/database-role-permissions.md)の許可一覧に記載する。許可一覧にない操作とGRANT OPTIONは禁止する。

| 対象 | 使う操作 |
|---|---|
| analyzable_articles・article_curations | 元記事の公開日時、本文、翻訳・要約の参照 |
| analyzed_articles | トレンドの集計、ブリーフィングの入力記事 |
| categories | カテゴリ一覧、監査に記録するカテゴリslug |
| trends_snapshots | 生成済みかの確認、トレンドの保存 |
| weekly_briefings | 生成済みかの確認、ブリーフィングの保存 |
| pipeline_events | トレンドの実行結果、ブリーフィングの投入・生成結果の監査 |

## ロール作成と付与

ロールは`backend/db_roles.json`に加え、既存のDBロール作成経路で作成する。新規環境の初期構築SQL、ローカルの初期化script、CIのロール作成、テスト用composeの接続値にも同名ロールを加え、ローカルとCIではNOLOGINで作成する。ロール作成の設定変更はcontractのmigrationと同じ変更に含められないため、付与より先に反映する。

付与は新しいAlembic revisionで行い、ロールが無ければ停止する。MIGRATION_KINDはcontractとし、lock_timeoutとstatement_timeoutは各5秒とする。downgradeは付与した権限だけを取り消し、ロールは残す。

## 切替

insights段のタスクロールは共通の権限境界がDBユーザーを限定しないため、bootstrapの変更は要らない。接続できるDBユーザーは本体のdb_usersで決まる。

1. ロールを作成し、付与のmigrationを適用する。
2. insights段のdb_usersにvector_insightsを加え、接続URLをvector_insightsへ切り替える。適用はタスクロールの接続許可と新しいtask definitionを登録するだけで、稼働中のタスクは変わらない。適用後のrolloutでサービスが新しいrevisionへ入れ替わるため、接続許可は必ず入れ替えより先に反映される。旧タスクが入れ替わるまで接続できるよう、vector_appへの接続許可は残す。
3. 全タスクが新しい定義に入れ替わり、切替後の最初の日次トレンド生成で認証・権限のエラーが無く、保存と監査が記録されたことを確認してから、db_usersからvector_appを外す。

3の前なら、接続URLを戻して適用とrolloutを行えば、vector_appでの接続に戻せる。週次のブリーフィング生成は切替後の観測を待たず、権限はローカル実DBの試験で保証する。

## 検証

- 動作: `local_tests/insights/`を新設し、製品のトレンド生成とブリーフィング生成を新ロールのEngineで動かす。LLMの呼び出し、frontendへの通知、subtaskの投入は境界で差し替え、データの準備と結果の観測は所有者の接続で行い、保存結果で判定する。
  - トレンド: 分析済み記事からsnapshotを保存し、成功を監査に記録する。生成済みの期間では保存しない。
  - ブリーフィング: 週次の起動がカテゴリを読んでsubtaskを投入し、監査に記録する。カテゴリ単位の生成がbriefingを保存して成功を記録する。入力記事の無い週と生成の失敗を監査に記録する。生成済みの週では保存しない。
  - 同時保存: 先に保存された行があるとき、INSERTが既存行を変えずに終わる。
- 許可一覧: `local_tests/permissions/test_insights_permissions.py`で、public・authの表・列・sequenceの権限が許可一覧と一致することを確認する。動作の試験は権限の過剰を検出できないため、この照合で最小権限を保証する。
- 共通の境界: `test_role_boundaries.py`に新ロールを加える。
- migration: `local_tests/migrations/`でupgrade・downgradeの往復、ロール不在時の停止、既存データとACLの維持を確認する。
- インフラ: 本体のTerraformテストで、insights段の接続URLと接続を許可するDBユーザーを確認する。

## 記事参照権限の追加

- Problem: 公開日時を参照するトレンド処理には元記事・curationの参照が必要だが、InsightsロールにはそのSELECTがない。
- Evidence: 公開日時はanalyzable_articlesに保存され、analyzed_articlesからarticle_curationsを介して参照する。既存のAgentロールもこの2表のSELECTを持つ。
- Invariants: この2表へSELECTだけを付与し、INSERT・UPDATE・DELETE・TRUNCATE・GRANT OPTIONは付与しない。他ロールの権限、既存データ、既存のInsights権限を維持する。
- Non-goals: トレンドの実行コード・選定ルール・DB構造・インデックスの変更、新しい表への自動付与。
- Done: 権限の許可一覧、migrationの往復、データと他ロールの権限維持を実DBで確認する。

`z30_grant_insights_publication` は `z29_grant_agent` に続くcontract migrationとして2表のSELECTを追加する。migration・権限テスト・仕様だけの先行PRとし、トレンドの実装は後続PRへ分ける。

適用順は先行PRのマージ、権限migrationの適用、後続アプリの反映とする。権限の追加だけなので旧アプリはそのまま動作できる。ロールバックは追加権限に依存するアプリを戻した後にdowngradeし、今回のSELECTだけを取り消す。
