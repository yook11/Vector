# ニュースソース戦略

Vector のニュースソース選定・段階的拡張の決定記録。

## 背景

- 既存ソース9本 (TechCrunch, FierceBiotech, BioPharma Dive, The Quantum Insider, Cointelegraph, Yahoo Finance, ITmedia, Hacker News, Alpha Vantage)
- 対象10ドメイン: AI / Robotics / Semiconductors / Next-gen Computing / Networks / Security / Space / Biotech / Materials / Energy
- Yahoo Finance パイプラインの品質問題、ドメインカバレッジの偏りが課題

## 決定の経緯

1. 外部リサーチ2本 ([prompt-split/README.md](../prompt-split/README.md)) を入力として検証リサーチを実施
2. リサーチの主張を4軸で並行検証 → [verification.md](./verification.md) に結果記録
3. スコアリングフレームワークを設計 → ドメインバランス・ユーザー価値の観点で修正
4. Phase 1/2 のソースセットを確定 → [phase-plan.md](./phase-plan.md) に記録

## 設計原則

- **既存を壊さず、ギャップを埋める追加方式**: 動いているソースを止めて未検証のものに入れ替えない
- **ユーザー価値優先**: ソース品質 (法的安全性等) だけでなく、Vectorのターゲットユーザーにとっての情報価値で判断
- **コンテンツ配信形式はスコアに過度に影響させない**: Trafilaturaパイプラインが稼働済みのため、スニペットRSSのペナルティは小さい
- **学術プレプリント (arXiv/bioRxiv) はニュースと別アーキテクチャ**: Phase 2 でアーキテクチャ拡張と合わせて導入

## 法的前提

- 内部パイプライン (フル本文取得 → Gemini解析 → embedding): 著作権法第30条の4 (情報解析目的) で保護
- ユーザー向け表示: 第32条 (引用) で保護 — リード文≤150字 + 要約 + 出典明示 + リンクバック
- Gemini要約は「翻案」ではなく「事実の摘要」として運用 (SmartNews/Gunosy/NewsPicks と同様の業界実践)
- `original_content` はAPI経由でユーザーに露出させない

## ファイル構成

| ファイル | 内容 |
|---------|------|
| [phase-plan.md](./phase-plan.md) | Phase 1a/1b/2 のソースセットと実装順序 |
| [verification.md](./verification.md) | リサーチ主張の検証結果サマリ |
| [scoring.md](./scoring.md) | スコアリングフレームワークとマスターリスト |
