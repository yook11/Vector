# APIのDBロール分離

## 作業定義

- Problem: APIがvector_appで接続している。vector_appはpublic全表の読み書き削除、新しい表への自動付与、auth.userの参照を持つため、外部からの要求を直接受けるAPIの不具合や侵害が、記事・分析結果・監査・Outboxの書き換えや利用者のアカウント情報の参照まで及ぶ。
- Evidence: APIの7系統のrouter（記事・カテゴリ・ウォッチリスト・トレンド・ブリーフィング・リサーチ・管理）から到達する実SQL、[スレッド表示時の期限回収](../agent/thread-open-deadline-recovery.md)、n3・y2・y4によるvector_appの付与、ECSのapi段のdb_usersと接続URL、task系の共通権限境界。
- Invariants:
  - APIのレスポンス・状態遷移・トランザクション境界を変えない。
  - 利用者が所有する行への限定は、引き続きコードの条件（user_idの一致）が担う。
  - DB IAM認証とTLSを維持する。
  - vector_appの権限と、vector_appで動く他の処理は変更しない。
  - 権限の正本は初期化SQLとmigrationとし、テストのfixtureでGRANTを足さない。
- Non-goals: vector_appの縮小と廃止、agent worker・insightsのロール分離、行単位の制限、管理機能の別ロール化、ORMが読み込む列の変更、期限回収のworkerへの移動、ローカル開発Composeのbackendの接続ロールの切替。
- Done: vector_apiに許可一覧どおりの権限を付与し、APIの全操作が新ロールで動くことをローカル実DBで確認する。ECSのapi段が新ロールで接続し、api段のタスクロールにvector_appへの接続許可が残らない。

## 権限の粒度

APIは利用者と運用者の要求を受け付けるプロセスで、触る表をニュース・成果物、利用者の所有データ、運用者の操作に分けて扱う。書き込みは利用者または運用者の操作に対応するものだけとし、回答生成・記事分析・インサイト生成など処理側の書き込みは持たない。

- ニュース・成果物: 表単位のSELECTだけとする。記事分析と同じく、中身は公開ニュースで隠す対象がない。
- 利用者の所有データ: 操作で必要な追加・削除と、操作で変わる列だけのUPDATEとする。SELECTは表単位とする。APIはスレッド・run・メッセージの行をまとめて読み込むため、列単位にはコードの変更が要る。
- 運用者の操作: 管理機能はAPIと同じプロセスで動き、DBからは一般の要求と区別できないため、同じロールに含める。
- 監査: pipeline_eventsは健全性画面が集計する5列のSELECTだけとする。payload・error_class・trace_idは読めず、書き込みも付与しない。
- 行ロック: PostgreSQLはFOR UPDATEに対象表の少なくとも1列のUPDATE権限を要求する。APIがロックするagent_threads・agent_runsは、操作で更新する列の権限で成立する。
- 削除: watchlist_entries・agent_threads・news_sourcesだけに付与する。スレッド削除に伴うメッセージ・run・出典の削除は外部キーの動作として参照側の表の所有者権限で動くため、子表には付与しない。
- 採番: news_sourcesのidのsequenceのUSAGEだけを付与する。APIが追加するagent系の表はgen_random_uuid()、ウォッチリストと利用枠は複合主キーで、sequenceを使わない。出典のidはsequenceで採番するが、APIは出典を追加しない。
- 接続: 接続先DBのCONNECTとpublicのUSAGEを直接付与する。authには何も付与しない。利用者はBFFが署名したJWTから得ており、auth.userを参照しない。
- 新しい表への自動付与、REFERENCES、TRUNCATEは付与しない。外部キーの検証は親表の所有者権限で動く。APIが新しい表・列を使うときはGRANTのmigrationを追加する。

更新する行の限定（所有者・状態の条件）と二重実行の防止は、GRANTではなくコードの条件・行ロック・一意制約が担う。

## 許可一覧と操作

表・列・sequenceの許可は[ロール権限仕様](../pipeline/database-role-permissions.md)の許可一覧に記載する。許可一覧にない操作とGRANT OPTIONは禁止する。下の表は、各表を使うAPIの操作を示す。

| 区分 | 対象 | 使う操作 |
|---|---|---|
| ニュース・成果物 | analyzable_articles・article_curations・analyzed_articles・news_sources・categories・weekly_briefings・trends_snapshots | 記事の一覧・詳細・類似、カテゴリ、ブリーフィング、トレンド、ウォッチ中の記事 |
| 利用者 | watchlist_entries | ウォッチの一覧・追加・解除 |
| 利用者 | agent_threads | 質問の開始、スレッドの一覧・詳細・削除 |
| 利用者 | agent_messages | 質問の保存、スレッドの詳細 |
| 利用者 | agent_message_sources | スレッドの詳細 |
| 利用者 | agent_runs | 開始、投入失敗の記録、取消、スレッド表示時の期限回収、runの参照 |
| 利用者 | agent_user_daily_quotas | 開始時の予約、取消・期限回収時の返却 |
| 運用者 | news_sources | ソースの登録・削除・有効化・無効化 |
| 運用者 | incomplete_articles・curation_noises・out_of_scope_articles・assessment_backfill_exclusions・embedding_backfill_exclusions・pipeline_events | 健全性画面 |

- 期限回収はスレッドを開く操作の一部で、期限切れのrunを確定させ、queuedだったrunの利用枠を返す。回収が書くのはstatusだけとする（[期限回収の更新列](#期限回収の更新列)）。
- assistant_message_idのUPDATEを持たないため、制約（completedのときだけ回答を持つ）と合わせて、APIは既存のrunを完了にできず、完了済みのrunの状態と回答の結び付けも書き換えられない。agent_runs・agent_messagesのINSERTは表単位のため、完了状態のrunを新しく挿入する余地は残る。
- 手動取得はnews_sourcesの読み取りとTaskiqへの投入だけで、上の許可に含まれる。
- 回答メッセージ・出典の書き込み、runの実行世代・開始記録・途中経過の更新、query_embedding_cache・outbox_eventsへのアクセスは付与しない。

## 期限回収の更新列

期限回収のUPDATEはstatusだけを書く（#442）。回収対象はqueued・runningのrunに限られ、DBの制約（completed⇔回答あり、failed⇔error_codeあり）により、assistant_message_idとerror_codeは回収の前から必ずNULLで、回収後のdeadline_exceededでもNULLのまま保たれる。回収後の値と[スレッド表示時の期限回収](../agent/thread-open-deadline-recovery.md)の契約は変わらない。

同じ回収処理をworkerの定期回収も使う。workerだけが通る期限切れ・ポリシー拒否の確定にある同じ代入は、APIの権限に関係しないため変更しない。

## ロール作成と付与

ロールは`backend/db_roles.json`に加え、既存のDBロール作成経路で作成する。新規環境の初期構築SQL、ローカルの初期化script、CIのロール作成、テスト用composeの接続値にも同名ロールを加え、ローカルとCIではNOLOGINで作成する。ロール作成の設定変更はcontractのmigrationと同じ変更に含められないため、付与より先に反映する。

付与は新しいAlembic revisionで行い、ロールが無ければ停止する。MIGRATION_KINDはcontractとし、lock_timeoutとstatement_timeoutは各5秒とする。downgradeは付与した権限だけを取り消し、ロールは残す。

## 切替

api段のタスクロールは共通の権限境界がDBユーザーを限定しないため、bootstrapの変更は要らない。接続できるDBユーザーは本体のdb_usersで決まる。

1. ロールを作成し、付与のmigrationを適用する。期限回収の変更（#442）は接続の切替より前にapi段で稼働させる。
2. api段のdb_usersにvector_apiを加え、接続URLをvector_apiへ切り替える。適用はタスクロールの接続許可と新しいtask definitionを登録するだけで、サービスはtask definitionの変更を無視するため稼働中のタスクは変わらない。適用後のrolloutでサービスが新しいrevisionへ入れ替わるため、接続許可は必ず入れ替えより先に反映される。旧タスクが入れ替わるまで接続できるよう、vector_appへの接続許可は残す。
3. 全タスクが新しい定義に入れ替わり、切替後の要求に認証・権限のエラーが無いことを確認してから、db_usersからvector_appを外す。

3の前なら、接続URLを戻して適用とrolloutを行えば、vector_appでの接続に戻せる。管理操作・投入失敗・期限回収は本番で頻繁に起きないため、これらの権限は本番の観測ではなくローカル実DBの試験で保証する。

## 検証

- 動作: `local_tests/api/`を新設し、製品のFastAPIアプリを新ロールのEngineで動かす。Redisを使う依存（実行状況の配信、実行の投入、締切の予約、SSEの同時接続枠）は境界で差し替え、認証はBFFと同じ形式のJWTをテスト用の鍵で署名する。データの準備と結果の観測は所有者の接続で行い、応答とDBの保存結果で判定する。
  - 閲覧: 記事の一覧・詳細・類似、カテゴリ、ブリーフィングの一覧・詳細、トレンドが準備したデータを返す。
  - ウォッチリスト: 追加・ID一覧・一覧・解除が保存結果に反映される。
  - リサーチ: 新規スレッドと既存スレッドでの開始、投入失敗の記録、queuedとrunningの取消、期限切れのrunを含むスレッドを開いたときの回収と利用枠の返却、スレッド削除でメッセージ・run・出典が消えること、runの参照。
  - 管理: ソースの登録・有効化・無効化・削除、健全性の2画面、手動取得。
- 期限回収の更新列: 既存の期限回収の試験で、回収後のassistant_message_id・error_codeがNULLであることを確認する（#442）。
- 許可一覧: `local_tests/permissions/test_api_permissions.py`で、public・authの表・列・sequenceの権限が許可一覧と一致することを確認する。動作の試験は権限の過剰を検出できないため、この照合で最小権限を保証する。個々の操作の成否は試験しない。
- 共通の境界: `test_role_boundaries.py`に新ロールを加える。
- migration: `local_tests/migrations/`でupgrade・downgradeの往復、ロール不在時の停止、既存データとACLの維持を確認する。
- インフラ: 本体のTerraformテストで、api段の接続URLと接続を許可するDBユーザーを確認する。
