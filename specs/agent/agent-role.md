# agentのDBロール分離

## 作業定義

- Problem: agent段（回答生成のworker）がvector_appで接続している。vector_appはpublic全表の読み書き削除、新しい表への自動付与、auth.userの参照を持つため、外部LLMの応答と外部検索の結果を扱うworkerの不具合や侵害が、記事・分析結果・利用者データ・監査の書き換えやスレッドの削除まで及ぶ。
- Evidence: workerの3タスク（回答生成、期限切れの定期回収、runごとの期限確認）から到達する実SQL、[APIのロール分離](../platform/api-role.md)と[スレッド表示時の期限回収](thread-open-deadline-recovery.md)、`local_tests/agent/`の実DB試験、n3・y2によるvector_appの付与、ECSのagent段のdb_usersと接続URL、task系の共通権限境界。
- Invariants:
  - runの状態遷移、実行世代による排他、回答・出典・handoffの保存結果、期限回収と利用枠の返却を変えない。
  - 利用者が所有する行と実行中のrunへの限定は、引き続きコードの条件（スレッドとrunの一致、実行世代）が担う。
  - DB IAM認証とTLSを維持する。
  - vector_appの権限を変更しない。
  - 権限の正本は初期化SQLとmigrationとし、テストのfixtureでGRANTを足さない。
- Non-goals: vector_appの縮小と廃止、APIのロールの変更、行単位の制限、期限回収の行ロックの範囲の変更、ポリシー拒否の記録の配線、ローカル開発Composeのbackendの接続ロールの切替。
- Done: vector_agentに許可一覧どおりの権限を付与し、workerの全操作が新ロールで動くことをローカル実DBで確認する。ECSのagent段が新ロールで接続し、agent段のタスクロールにvector_appへの接続許可が残らない。

## 権限の粒度

DBの権限の変更はmigrationと本番への適用を伴い重い。agent機能の変更に柔軟に対応できるよう、読み取りとrunの更新は少し広めに付与し、それ以外の書き込みは現在の操作に絞る。

- 読み取り: agentの表（agent_threads・agent_messages・agent_message_sources・agent_runs・agent_user_daily_quotas・query_embedding_cache）と、公開ニュース・成果物の表（analyzable_articles・article_curations・analyzed_articles・categories・news_sources・weekly_briefings・trends_snapshots）は表単位のSELECTとする。
- run: agent_runsのUPDATEは表単位とする。状態と回答・error_codeの整合はCHECK制約が担う。
- スレッド: UPDATEはupdated_at・research_handoffだけとする。user_idを書き換えるとスレッドが他の利用者に移る。
- 会話履歴・埋め込みキャッシュ: agent_messages・agent_message_sources・query_embedding_cacheの書き込みはINSERTだけとする。
- 利用枠: agent_user_daily_quotasの書き込みはused_count列のUPDATEだけとする。予約はAPIが行う。
- 作成・削除: agent_threads・agent_runs・agent_user_daily_quotasのINSERTと、全表のDELETEは付与しない。
- 行ロック: 期限回収のFOR UPDATEはagent_threadsの行も対象にし、updated_at・research_handoffのUPDATEで成立する。
- 採番: agent_message_sources・query_embedding_cacheのidのsequenceのUSAGEだけを付与する。
- 接続: 接続先DBのCONNECTとpublicのUSAGEを直接付与する。auth・pipeline_events・outbox_eventsには何も付与しない。
- 新しい表への自動付与、REFERENCES、TRUNCATEは付与しない。新しい表を使うときはGRANTのmigrationを追加する。

更新する行の限定と二重実行の防止は、GRANTではなくコードの条件・行ロック・一意制約が担う。

## 許可一覧と操作

表・列・sequenceの許可は[ロール権限仕様](../pipeline/database-role-permissions.md)の許可一覧に記載する。許可一覧にない操作とGRANT OPTIONは禁止する。下の表は、各表を使うworkerの操作を示す。

| 対象 | 使う操作 |
|---|---|
| agent_runs | 開始（実行世代の更新）、継続判定、回答開始の記録、再生成の許可、完了、失敗の記録、期限切れの確定 |
| agent_threads | 質問の所有者とhandoffの読み出し、完了時の最終活動時刻とhandoffの更新、期限回収の行ロック |
| agent_messages | 質問と履歴の読み出し、回答の保存 |
| agent_message_sources | 出典の保存 |
| agent_user_daily_quotas | 開始時に期限を過ぎていたrunと、期限回収したqueuedのrunの利用枠の返却 |
| query_embedding_cache | 質問の埋め込みの参照と保存 |
| analyzed_articles・article_curations・analyzable_articles・categories | 内部検索 |
| news_sources・weekly_briefings・trends_snapshots | 現在の操作では使わない |

- APIとの分担は操作単位とする。APIはスレッド・runの作成、取消、投入失敗の記録、スレッドの削除、スレッド表示時の期限回収を行い、workerはrunの開始から完了・失敗までと、定期・runごとの期限回収を行う。
- 期限回収のSQLはAPIと共用し、回収の更新はstatusだけを書く（#442）。workerだけが通る期限切れの確定はassistant_message_id・error_codeにもNULLを代入するが、agent_runsの表単位のUPDATEに含まれる。
- 期限確認の予約はRedisだけを使い、DBの権限は要らない。

## ロール作成と付与

ロールは`backend/db_roles.json`に加え、既存のDBロール作成経路で作成する。新規環境の初期構築SQL、ローカルの初期化script、CIのロール作成、テスト用composeの接続値にも同名ロールを加え、ローカルとCIではNOLOGINで作成する。ロール作成の設定変更はcontractのmigrationと同じ変更に含められないため、付与より先に反映する。

付与は新しいAlembic revisionで行い、ロールが無ければ停止する。MIGRATION_KINDはcontractとし、lock_timeoutとstatement_timeoutは各5秒とする。downgradeは付与した権限だけを取り消し、ロールは残す。

## 切替

agent段のタスクロールは共通の権限境界がDBユーザーを限定しないため、bootstrapの変更は要らない。接続できるDBユーザーは本体のdb_usersで決まる。

1. ロールを作成し、付与のmigrationを適用する。
2. agent段のdb_usersにvector_agentを加え、接続URLをvector_agentへ切り替える。適用はタスクロールの接続許可と新しいtask definitionを登録するだけで、稼働中のタスクは変わらない。適用後のrolloutでサービスが新しいrevisionへ入れ替わるため、接続許可は必ず入れ替えより先に反映される。旧タスクが実行中のrunを終えて入れ替わるまで接続できるよう、vector_appへの接続許可は残す。
3. 全タスクが新しい定義に入れ替わった後、次を確認してからdb_usersからvector_appを外す。
   - 毎分の期限回収が認証・権限のエラーなく動く。回収するrunが無くても、行ロック付きの読み取りを実行する。
   - 切替後に内部検索を通ったリサーチが完了し、回答と出典が保存される。
   - 埋め込みキャッシュの参照・保存の失敗（`vector.agent.internal_retrieval.query_embedding_cache`）が増えていない。キャッシュの失敗はrunを失敗させないため、runの結果ではなくこのメトリクスで確認する。

3の前なら、接続URLを戻して適用とrolloutを行えば、vector_appでの接続に戻せる。利用枠の返却は本番で頻繁に起きないため、権限は本番の観測ではなくローカル実DBの試験で保証する。

## 検証

- 動作: `local_tests/agent/`の製品の接続（試験内のworkerのEngineと、別プロセスのworker）をvector_agentへ切り替え、結果の観測を所有者の接続に移す。既存の試験（回答生成の工程、完了と期限回収の競合、実行世代の排他、重複配送、workerの停止後の回収）が新ロールで成功することを確認し、既存の試験が通らない次の操作を加える。
  - 内部検索: 準備した分析済み記事が検索され、出典として保存される。
  - 埋め込みキャッシュ: 初回の質問の埋め込みが保存され、同じ質問では埋め込みを再計算しない。参照・保存の失敗はrunを失敗させないため、保存結果と埋め込みの呼び出しで判定する。
  - handoff: 完了時にスレッドへhandoffが保存される。
  - 利用枠: queuedのまま期限を過ぎたrunを回収すると、利用枠が返却される。
- 許可一覧: `local_tests/permissions/test_agent_permissions.py`で、public・authの表・列・sequenceの権限が許可一覧と一致することを確認する。動作の試験は権限の過剰を検出できないため、この照合で付与の範囲を保証する。個々の操作の成否は試験しない。
- 共通の境界: `test_role_boundaries.py`に新ロールを加える。
- migration: `local_tests/migrations/`でupgrade・downgradeの往復、ロール不在時の停止、既存データとACLの維持を確認する。
- インフラ: 本体のTerraformテストで、agent段の接続URLと接続を許可するDBユーザーを確認する。
