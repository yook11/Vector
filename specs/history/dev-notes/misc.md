# 開発メモ圧縮記録 — 単発の学び (3月-5月)

個人開発メモ (Claude.ai 対話ログ + ローカルノート、リポジトリ外) の圧縮記録のうち、
どの変化の筋にも属さない一回的な獲得・事故・運用知見を時系列で集めたもの。
原本のコードやコマンドは転記せず、当時考えていたこと・決めたこと・その後の帰結だけを言語化して残す。
全体の地図は [README.md](README.md)。

## DB のメンタルモデル確立 (3/26-27)

- **models = あるべき姿の宣言 / Alembic = 現状からの差分実行** という基本構図を対話で固めた。
  models を変えただけでは DB に反映されない。`create_all()` はテスト専用 (履歴を持たず既存テーブルの変更ができない)。
- マイグレーション運用: 3 段階パターン (add nullable → migrate data → drop + enforce)、
  旧カラム DROP はコード変更と動作確認を終えた最後、DB はロールバックが効かないという感覚。
- 失敗からの学び: Better Auth の auth スキーマ分離と UUID で手間取った原因を
  「プラン作成前の公式ドキュメント調査不足」と特定した。以後のリサーチ義務化の起点
  ([ai-collaboration.md](ai-collaboration.md))。

## Git 基礎の確立 (3/29-30)

- commit 粒度 (1 論理変更 = 1 コミット)、main + feature の 2 層ブランチ、Conventional Commits、
  PR の書き方 (概要・変更内容・動機・確認事項)、merge 方式の使い分け、ローカル/リモートの区別、
  マージ済みブランチの掃除 (`--merged main` / `fetch --prune`)、stash 退避。
- 失敗の言語化: better-auth ブランチに UI 刷新と DeclarativeBase 移行が混在した。
  再発防止は「作業を始める前にブランチのスコープを一言定義する」。
  混在に気づいた時点では無理に分割せず、PR の Description に両方を明記して進める現実解も学んだ。

## 4 月の単発

- lockfile 設定ミスで Next.js のバージョンが意図せず更新されクラッシュ (4/2)。
- selectinload (1対N) / joinedload (N対1) の使い分け基準をクリップ (4/8)。

## 運用ツールとデプロイの手触り (5/6, 5/22)

- /red-team コマンドを実走させ、構造的な改善案 10 件を抽出した: probe budget の明示、handoff の Phase 2 検証計画の構造化、
  破壊的操作の可逆性段階表、scanner 起動プロンプトのテンプレート化、レポート末尾の operator 向け next actions、
  単独 severity と「チェーンの種としての潜在 severity」の分離、など。自作の運用ツールも作って終わりにせず、
  実走で出た歪みを構造改善として記録する姿勢。
- デプロイ前の必須アクションをメモ (5/22): 設定の production narrowing による fail-fast のため、内部 URL の secret が
  正しい形式でないと起動時にクラッシュする。fail-fast 設計は「デプロイ前に secret を先行設定する」という
  運用手順を要求する、という気づき。

帰結: red-team の現行定義には probe budget の明示・handoff の検証アプローチ節・severity と chain seed の分離が
反映済み。可逆性段階表・起動テンプレート化・operator 向け next actions 節は未反映のまま残っている。

## 関連文書

- [README.md](README.md) — 全体の地図
- [ai-collaboration.md](ai-collaboration.md) — Better Auth の失敗が接続するリサーチ義務化の弧
