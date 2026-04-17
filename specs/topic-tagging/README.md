# Topic Tagging — 記事トピック分類の再設計

> ステータス: 設計中
> 軸: 既存 Keyword 機能のアップデート

## 達成したいこと

投資家から見て「このニュースは何を報じているのか」が明確なタグをつけ、
そのトピックのニュースにすぐアクセスできるようにする。

## 現行 Keyword の問題

人間が事前定義した 72 個の抽象キーワードから AI に最大 3 個選ばせる方式:

1. **精度が悪い**: キーワードが抽象的すぎて記事の具体性に合わない。関係ないタグがつく
2. **概念の混在**: 分類タグ (drug discovery) と固有名詞 (CRISPR) が同一リストに同居
3. **語彙が閉じている**: 選択肢から選ばせるアプローチ自体が精度を下げる一因

## 解決方針

- 事前定義リストを廃止し、AI が記事ごとに Topic を自由生成する
- 正規化 (英語小文字化・記号統一) と完全一致で既存 Topic に収束させる
- Category → Topic → Article の 1:1 階層で帰属を一意にする

## ドキュメント

| ファイル | 内容 | ステータス |
|---|---|---|
| [concept-model.md](concept-model.md) | Topic の定義、Category との関係、正規化ルール | 確定 |
| [impact-map.md](impact-map.md) | 変更の全体マップ (全レイヤーの影響範囲) | 確定 |
| [ai-pipeline.md](ai-pipeline.md) | プロンプト設計、パース、正規化、永続化フロー | 確定 |
| data-model.md | topics テーブル設計、マイグレーション計画 | 未着手 |
| migration.md | 既存データ移行、旧テーブル削除の手順 | 未着手 |

## 関連

- [trend-detection/](../trend-detection/) — 最新ワード検出 (別機能・並行で設計中)
- [domain-module-restructure.md](../domain-module-restructure.md) — analysis/ パッケージ構成
