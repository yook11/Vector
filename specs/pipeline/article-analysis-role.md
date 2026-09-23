# 記事単位AI分析のDBロール分離

## 作業定義

- Problem: curation・assessment・embeddingのLambdaがvector_appで接続している。vector_appはpublic全表の読み書き削除と新しい表への自動付与を持つため、分析の不具合や侵害が記事の削除や分析以外のデータの書き換えまで及ぶ。
- Evidence: 3段のrepository・監査・Outbox発行の実SQL、n3によるvector_appの付与、z14・z22による収集ロールの付与、3つのconsumerの実行ポリシーと権限境界、ローカル実DBテスト。
- Invariants:
  - 3段の保存結果・監査・Outboxを同一トランザクションで確定する振る舞いを変えない。
  - DB IAM認証とTLSを維持する。
  - vector_appの権限と、vector_appで動く他の処理は変更しない。
  - 権限の正本は初期化SQLとmigrationとし、テストのfixtureでGRANTを足さない。
- Non-goals: vector_appの縮小、他の処理のロール分離、IAMロールの概念統合、行単位の制限、段ごとのロール分割。
- Done: vector_article_analysisに許可一覧どおりの権限を付与し、3段が新ロールで動くことをローカル実DBで確認する。3つのLambdaが新ロールで接続し、実行ポリシーと権限境界にvector_appへの接続許可が残らない。

## 権限の粒度

分析が触る表を、記事・分析結果の表と、全段が書く監査・Outboxに分けて扱う。

- 読み取り: 記事・分析結果・カテゴリの表は表単位のSELECTとする。中身は公開ニュースで隠す対象がなく、列単位では読む列が増えるたびにGRANTが要る。
- 監査・Outbox: 他の段の記録やイベント本文を含むため、INSERTと、ORMがRETURNINGで受け取る列のSELECTだけとする。payloadは読めない。
- 追加: INSERTは既存行を変えないため表単位とする。
- 更新: `analyzed_articles.embedding`列だけとする。保存前の行ロック（FOR UPDATE）もこの列の権限で成立する。
- 削除: 付与しない。
- 採番: INSERTする表のidはsequenceで採番するため、そのsequenceのUSAGEだけを付与する。
- 新しい表への自動付与はしない。分析が新しい表・列を使うときはGRANTのmigrationを追加する。
- 外部キーの検証は親表の所有者権限で動くため、REFERENCESは付与しない。

更新する行の限定と二重保存の防止は、GRANTではなくコードの条件と一意制約が担う。3段は同じロールで動くため、段の間の書き込みは区別しない。

## 許可一覧

表・列・sequenceの許可は[ロール権限仕様](database-role-permissions.md)の許可一覧に追加する。許可一覧にない操作とGRANT OPTIONは禁止する。

## ロール作成と付与

ロールは`backend/db_roles.json`に加え、既存のDBロール作成経路で作成する。新規環境の初期構築SQL、ローカルの初期化script、CIのロール作成にも同名ロールを加え、ローカルとCIではNOLOGINで作成する。ロール作成の設定変更はcontractのmigrationと同じ変更に含められないため、付与より先に反映する。

付与は新しいAlembic revisionで行い、ロールが無ければ停止する。MIGRATION_KINDはcontractとし、lock_timeoutとstatement_timeoutは各5秒とする。downgradeは付与した権限だけを取り消し、ロールは残す。

## 切替

1. ロールを作成し、付与のmigrationを適用する。
2. 3つのconsumerの権限境界と実行ポリシーに新ロールへの接続許可を加え、接続URLを新ロールへ切り替える。権限境界の変更を先に適用し、更新前の設定で動く実行のためにvector_appへの接続許可は残す。
3. 3つのLambdaの設定更新が完了し、切替後の処理に認証・権限のエラーが無いことを確認してから、権限境界と実行ポリシーからvector_appへの接続許可を外す。

## 検証

- 許可一覧: `local_tests/permissions/test_article_analysis_permissions.py`で、表・列・sequenceの権限が許可一覧と一致することを確認する。禁止操作（削除、embedding以外の列の更新、元記事・カテゴリ・他の処理の表への書き込み、監査・Outboxの本文参照）はInsufficientPrivilegeErrorだけを成功とする。成功する操作は各段の動作テストで確認し、権限テストでは重ねない。
- 共通の境界: `test_role_boundaries.py`に新ロールを加える。
- curation: `local_tests/curation/`を新設し、migration適用済みDBで製品のhandlerを新ロールで動かす。signalと判定した記事はcuration結果・監査・Outboxが保存され、noiseと判定した記事はnoise・監査が保存されてOutboxを発行しないことを確認する。AIとの通信は境界で差し替える。
- assessment・embedding: 既存のローカル実DBテストの接続を新ロールへ切り替える。
- migration: `local_tests/migrations/`でupgrade・downgradeの往復、ロール不在時の停止、既存データとACLの維持を確認する。
- インフラ: 3つのconsumerと権限境界のTerraformテストで、接続URLと接続許可の対象ユーザーを確認する。
