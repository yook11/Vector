# 救済（backfill）のDBロール分離

## 作業定義

- Problem: curation・assessment・embedding・completionのbackfill Lambdaがvector_appで接続している。vector_appはpublic全表の読み書き削除と新しい表への自動付与を持つため、救済の不具合や侵害が救済と関係のない業務データの書き換えや削除まで及ぶ。
- Evidence: 4工程の対象抽出・行ロック・期限切れ整理・監査の実SQL、n3によるvector_appの付与、z22〜z24による専用ロールの付与、backfillの実行ポリシーと権限境界、ローカル実DBテスト。
- Invariants:
  - 4工程の対象抽出・期限切れ整理・再投入契約と、整理と監査を同一トランザクションで確定する振る舞いを変えない。
  - DB IAM認証とTLSを維持する。
  - vector_appの権限と、vector_appで動く他の処理は変更しない。
  - 権限の正本は初期化SQLとmigrationとし、テストのfixtureでGRANTを足さない。
- Non-goals: vector_appの縮小、他の処理のロール分離、行ロックの取り方の変更、行単位の制限、工程ごとのロール分割、接続先ユーザー以外のIAM・Schedulerの変更。
- Done: vector_backfillに許可一覧どおりの権限を付与し、4工程の再投入と期限切れ整理が新ロールで動くことをローカル実DBで確認する。4つのLambdaが新ロールで接続し、実行ポリシーと権限境界にvector_appへの接続許可が残らない。

## 権限の粒度

backfillは記事・分析結果・未完成記事を読み、期限切れの整理として記事の削除、除外行の追加、未完成記事のclosedへの更新を行い、全工程で監査を書く。Outboxは使わない。

- 読み取り: 対象抽出で読む記事・分析結果・除外・未完成記事・ニュースソースの表は表単位のSELECTとする。中身は公開ニュースと取得元の設定で、記事分析と同じく隠す対象がない。
- 監査: INSERTと、ORMがRETURNINGで受け取るid・occurred_atのSELECTだけとする。payloadは読めない。
- 削除: analyzable_articlesだけに付与する。子表の削除とpipeline_events.article_idのNULL化は外部キーの動作として参照側の表の所有者権限で動くため、子表には付与しない。
- 追加: 除外2表は表単位とする。INSERTは既存行を変えないため。
- 更新: incomplete_articlesのstatus・leased_until・updated_at列だけとする。
- 行ロック: 整理の前にanalyzable_articles・article_curations・analyzed_articlesの対象行をFOR UPDATEでロックする。PostgreSQLはこれに対象表の少なくとも1列のUPDATE権限を要求し、DELETE権限では代われないため、3表のid列だけにUPDATEを付与する。idは書き換えても本文・分析結果・時刻が変わらず、参照されている行の変更は外部キーが拒否する。参照されていない行のidは書き換えられ、採番より先の値にされると後の追加が一意制約違反になるが、行ロックのための付与として許容する。incomplete_articlesのロックは更新する列の権限で成立する。
- 採番: pipeline_eventsのidのsequenceのUSAGEだけを付与する。除外2表は採番しない。
- 接続: 接続先DBのCONNECTとpublicのUSAGEを直接付与する。
- 新しい表への自動付与、REFERENCES、TRUNCATEは付与しない。backfillが新しい表・列を使うときはGRANTのmigrationを追加する。

整理する行の限定と二重整理の防止は、GRANTではなくコードの条件・行ロック・一意制約が担う。4工程は同じロールで動くため、工程の間の書き込みは区別しない。

## 許可一覧

表・列・sequenceの許可は[ロール権限仕様](database-role-permissions.md)の許可一覧に追加する。許可一覧にない操作とGRANT OPTIONは禁止する。

## ロール作成と付与

ロールは`backend/db_roles.json`に加え、既存のDBロール作成経路で作成する。新規環境の初期構築SQL、ローカルの初期化script、CIのロール作成、テスト用composeの接続値にも同名ロールを加え、ローカルとCIではNOLOGINで作成する。ロール作成の設定変更はcontractのmigrationと同じ変更に含められないため、付与より先に反映する。

付与は新しいAlembic revisionで行い、ロールが無ければ停止する。MIGRATION_KINDはcontractとし、lock_timeoutとstatement_timeoutは各5秒とする。downgradeは付与した権限だけを取り消し、ロールは残す。

## 切替

1. ロールを作成し、付与のmigrationを適用する。
2. backfillの権限境界に新ロールへの接続許可を加えて先に適用し、実行ポリシーと4つのLambdaの接続URLを新ロールへ切り替える。更新前の設定で動く実行のためにvector_appへの接続許可は残す。
3. 4つのLambdaの設定更新が完了し、切替後の実行に認証・権限のエラーが無いことを確認してから、権限境界と実行ポリシーからvector_appへの接続許可を外す。

期限切れの整理は毎回の実行で起きないため、整理の権限は本番の観測ではなくローカル実DBの試験で保証する。

## 検証

- 動作: `local_tests/backfill/`で、4工程それぞれを実入口から新ロールで1回起動し、期限切れ整理と再投入が1回の実行で確定することを確認する。期限切れと期間内の対象を用意し、実行後のDBで記事の削除、除外行、未完成記事のclosed、監査行を、送信内容で再投入を確認する。item監査はbest-effortで失敗しても実行が成功するため、例外が無いことではなく保存結果で判定する。テストデータの投入は所有者の接続のままとし、接続の観測は新ロールで行う。
- 許可一覧: `local_tests/permissions/test_backfill_permissions.py`で、表・列・sequenceの権限が許可一覧と一致することを確認する。動作の試験は権限の過剰を検出できないため、この照合で最小権限を保証する。個々の操作の成否は試験しない。
- 共通の境界: `test_role_boundaries.py`に新ロールを加える。
- migration: `local_tests/migrations/`でupgrade・downgradeの往復、ロール不在時の停止、既存データとACLの維持を確認する。
- インフラ: backfillの本体と権限境界のTerraformテストで、接続URLと接続許可の対象ユーザーを確認する。
