# 埋め込みコンテナ arm64 統一プラン

## 目的
- Apple Silicon 開発環境で TEI が emulation なしに動く状態にする
- 本番 AWS (arm64/Graviton) との parity を確保する
- 作業規模最小で解決する（自前ビルド回避）

## 背景
- 現状 `docker-compose.yml` の `embedding` サービスは `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9` を `platform: linux/amd64` で強制しており、Apple Silicon では Rosetta emulation 経由で動作が不安定
- AWS デプロイ構成は ECS Fargate arm64 (Graviton) 統一方針で確定済み（`project_aws_deployment_architecture.md`）

## 前提・調査済み事実
- TEI v1.9.3 は `Dockerfile-arm64` を 2026-03 に merge 済み（PR #827）
- ghcr.io に `cpu-arm64-latest` / `cpu-arm64-sha-*` タグで arm64 イメージが配信済み
- `cpu-1.9` 等の数値タグは amd64 only、multi-arch manifest ではない
- ruri-v3-310m は ModernBERT-Ja ベース。TEI 公式サポート明記なし → 要実機検証
- 他サービス（frontend, backend, pgvector/pgvector:pg16, redis:7-alpine）は全て arm64 対応済み

## 作業ステップ

### Step 1: ブランチ作成
- `main` から `chore/embedding-arm64-switch` を切る

### Step 2: docker-compose.yml 修正（最小変更）
- `embedding` サービスの以下 2 箇所を変更
  - `image: ...:cpu-1.9` → `:cpu-arm64-latest`
  - `platform: linux/amd64` の行を削除
- 他のサービスは無変更

### Step 3: Mac で起動検証
- `docker compose up -d embedding`
- `docker compose logs embedding` で ModernBERT-Ja のロード成功を確認
- `curl -X POST http://localhost:<port>/embed -H "Content-Type: application/json" -d '{"inputs":"検索文書: テスト"}'` で 768 次元ベクトルが返ることを確認
- `docker inspect` で Architecture が `arm64` であることを確認

### Step 4: 結果分岐
- **Step 3 成功** → Step 5 に進む
- **Step 3 失敗（ModernBERT-Ja 非対応エラー等）** → 本プラン停止、案A-2（自前 Dockerfile ビルド）を別プランで起票

### Step 5: タグ固定
- `cpu-arm64-latest` は本番不適（不意に差し変わる）
- ghcr.io の最新安定タグを確認し `cpu-arm64-sha-<sha>` 形式で固定
- dev/prod ともに同じ sha を使う

### Step 6: PR 作成
- タイトル: `chore(infra): switch embedding image to arm64`
- 本文: AWS Graviton 統一方針の一環、Apple Silicon dev parity 改善

## 検証基準（完了条件）
1. Mac 上で `docker compose up` が emulation 警告なしで通る
2. TEI コンテナが ruri-v3-310m を正常にロードする
3. `/embed` が 768 次元を返す
4. `docker compose ps` で全サービス healthy

## ロールバック
- 検証失敗時は docker-compose.yml を revert するだけ
- 影響極小

## 影響範囲
- 変更ファイル: `docker-compose.yml` のみ
- backend 側コード（embedder 等）は無変更（httpx で TEI を叩く構成のまま）
- CI/CD 影響なし（本番 CDK は未構築）

## 非対象
- `project_embedding_migration.md` の Step 3-5（RuriEmbedder 実装、Alembic halfvec 移行、キャッシュキー変更）は別作業
- AWS CDK スタック構築は別プラン
- 他サービスの arm64 対応チェック（既に multi-arch 対応済みのため不要）

## リスク
- **主**: ruri-v3-310m が TEI で動かない可能性 → Step 4 で分岐判断
- **副**: `cpu-arm64-latest` の breaking change → Step 5 で sha 固定し緩和
