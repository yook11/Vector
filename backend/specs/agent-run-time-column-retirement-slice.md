# Agent run時刻列の削除

## Problem / Evidence

`started_at`・`completed_at`は期限・epoch・quotaの判断に不要で、アプリ・ORM・E2E seed・通常fixtureからの依存は除去済み。
先行PRではNULL許容の2列と既存値を残した。今回は`z18_agent_run_deadline`の次に
`z19_drop_run_time_columns`（contract）を追加して、物理列を削除する。
過去仕様の開始・終端時刻を記録する契約は、本仕様で置き換える。

## Invariants

- runの開始・終端時刻は記録しない。2列の既存値はDROP時に破棄し、退避しない。
- 2列以外のschema・データは変更しない。
- run受付の`created_at`、受理期限の`deadline_at`、比較用のDB時刻`now`は維持する。
- deadline境界、epoch、結果保存、terminal保護、quota、commit後通知の契約は変更しない。
- 欠損件数`running_without_started_at_count`とその警告だけを廃止し、quotaの観測は残す。
- failure・enqueue failure・policy block・cancelの時刻記録専用`now`引数は廃止する。
  作成・start・complete・sweepの`now`と、回答message・threadの時刻更新は残す。
- downgradeは`TIMESTAMPTZ NULL`・defaultなしの2列を復元するが、削除済みの値は復元できない。
- `IF EXISTS`・`CASCADE`は使わず、列の欠落やDROPを妨げる依存関係を隠さない。
  lock／statement timeoutは各5秒とし、同一transactionで実行する。

## Tests / Done

- 新規は隔離したz18時点の列構成で行うmigrationの往復テスト1本とする。
  値あり／NULLの行を用意し、`upgrade → downgrade → upgrade`で2列の削除・空の列としての復元と、残る列・データの維持を確認する。
- 削除済みの「旧列あり／なし」互換テストは再追加しない。AST・SQLの形・Postgres自体の動作だけを検査するテストは増やさない。
- 過去のmigration履歴テストと、deadline・epoch・quota・結果保存の既存回帰テストは維持する。
- lint・format確認・backend unit・`make test-integration`を通し、migration分類とcontractの変更範囲を確認する。

## Non-goals / Release

- このPRはmigration・必要なテスト・本仕様書のみ。アプリ・公開API・frontend・デプロイ基盤は変更しない。
- 過去のmigrationとその履歴テスト、SSE・トレース等の同名時刻は変更しない。
- マージ前に、削除PRの直前main SHAがproxy以外の全対象サービスへ反映済みで、旧プロセスが残っていないことと、ledgerがそのschemaに一致していることを確認する。
  先行PRのマージだけでは本番反映済みとみなさず、mainが進んだ場合は直前SHAの反映状況を再確認する。
- 本作業は日本語のコミット・独立PR作成まで。本番反映状況は未確認で、マージ・migration適用・デプロイは行わない。
