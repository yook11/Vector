# Agent run時刻列への依存除去

## Problem / Evidence

`started_at`・`completed_at`は期限・epoch・quotaの判断に不要だが、書き込みとORMの読み取りが残っている。
両列は既存migrationでNULL許容になっているため、列を残したままアプリの依存を先に外す。
過去仕様の開始・終端時刻を記録する契約は、本仕様で置き換える。

## Invariants

- runの開始・終端時刻は記録しない。既存の値は消去・更新しない。
- アプリ・E2E seed・通常fixtureから2列への依存を除き、ORMの暗黙の取得もなくす。
- run受付の`created_at`、受理期限の`deadline_at`、比較用のDB時刻`now`は維持する。
- deadline境界、epoch、結果保存、terminal保護、quota、commit後通知の契約は変更しない。
- 欠損件数`running_without_started_at_count`とその警告だけを廃止し、quotaの観測は残す。
- failure・enqueue failure・policy block・cancelの時刻記録専用`now`引数は廃止する。
  作成・start・complete・sweepの`now`と、回答message・threadの時刻更新は残す。

## Tests / Done

- 既存テストから旧列のfixture・期待値のみを除き、deadline sweepの2本・6ケースと回帰保証を維持する。
- 列の有無を切り替える移行互換テストは持たない。物理削除後のライフサイクルはDROP側の既存テストが担う。
- lint・format・unit・integrationを通す。

## Non-goals / Release

- このPRはアプリ変更のみ。migration・物理列・既存データ・公開API・frontend・デプロイ基盤は変更しない。
- 過去のmigrationとその履歴テスト、SSE・トレース等の同名時刻は変更しない。
- ORMと物理schemaが一時的に異なるのは意図した移行状態であり、このPRでDROPを自動生成しない。
- 次のPRで列をDROPする前に、本PRを全対象サービスへ反映し、旧プロセスが残っていないことを確認する。
