# Vector

> 海外テックニュース収集・AI翻訳・投資分析ダッシュボード

次世代コンピューティング、マテリアル・インフォマティクスなど、日本では情報が少ない先端分野の海外ニュースを自動収集し、AI で翻訳・要約・インパクト分析を行う投資ダッシュボードです。

## 画面プレビュー

Vector は、海外の先端テックニュースを自動収集し、AI で日本語に翻訳・要約したうえで、投資判断に必要な要点・背景・トレンドを確認できるダッシュボードです。

![カテゴリ別に収集された海外テックニュースを、日本語の要点付きで一覧できるニュースダッシュボード](docs/assets/readme/01-dashboard.png)

## 主な画面

| ニュース詳細 | ブリーフィング詳細 |
|---|---|
| ![AI が翻訳・要約した記事詳細画面。要点と背景文脈を確認できる。](docs/assets/readme/02-article-detail.png) | ![週次ブリーフィングの詳細画面。複数記事から生成された市場・技術動向の要約を読める。](docs/assets/readme/05-briefing-detail.png) |
| AI が記事を翻訳・要約し、要点と背景文脈を整理する。 | 複数記事をもとに、週次の市場・技術動向を読み物として整理する。 |


## 開発と設計への向き合い方

Vector は、最初から明確な設計思想を持って作り始めたアプリではありません。立ち上げ当初は、AI エージェントが生成したコードを十分に理解できないまま承認することも多く、まずは動くものを作るところから始まりました。

そこから、技術書を読み、既存実装をレビューし、実装中の失敗を振り返る中で、少しずつ「自分が何を大事にして設計するのか」を言語化してきました。

その考え方の変化と、現在の設計は目的別に以下へまとめています。

- 設計思想がどう変わってきたか → [docs/design-journey/](docs/design-journey/)
- 現在のアーキテクチャと主要な設計判断 → [docs/architecture.md](docs/architecture.md)
- AI エージェントとの分担や検証の進め方 → [docs/how-i-build-with-ai.md](docs/how-i-build-with-ai.md)

## 解決する課題

- 海外テックニュースは英語記事が多く、日本語話者の投資家が継続的に追うには負荷が高い
- 日々の記事は断片的で、AI・半導体・宇宙などの分野ごとに「今週何が起きたのか」を把握しづらい
- 投資判断の前段で必要な要点・背景・流れを拾うために、複数の記事を読み比べる時間がかかる

## 主要機能

- テックニュースの自動収集
- AI による日本語翻訳・要約・背景整理
- カテゴリ別の記事一覧とフィルタリング
- 関連記事推薦
- 週次 LLM ブリーフィング
- 注目ワード / 急上昇ワードの集計

## 技術スタック

| 領域 | 採用技術 |
|---|---|
| フロントエンド | Next.js 16 (App Router / BFF)・React 19・TypeScript・Tailwind CSS v4・shadcn/ui |
| 認証 | Better Auth (frontend BFF で完結) |
| バックエンド | Python 3.13・FastAPI・Pydantic / SQLModel・Alembic |
| 非同期処理 | taskiq (worker / scheduler)・Redis (queue / レート制限) |
| データ | Neon PostgreSQL・pgvector (768次元ベクトル検索) |
| AI | Gemini (翻訳・要約・構造化)・DeepSeek (重要度・投資文脈分析) |
| 基盤・可観測性 | Fly.io (nrt)・Docker Compose・Logfire (OpenTelemetry)・GitHub Actions |

## Architecture

Vector は、ブラウザから直接到達できる入口を Next.js BFF に寄せ、backend API と worker 群を内部側に閉じる構成です。
本番環境では Fly.io の 5 app と Neon PostgreSQL で動作しています。
公開リポジトリ内の `fly*.toml` は構成を説明するための placeholder 付き設定です。実際の app 名、内部 URL、デプロイ手順は private な運用情報として管理しています。

```mermaid
flowchart TB
    Browser([Browser])
    News[("外部ニュース源")]
    Neon[("Neon PostgreSQL<br/>アプリ・分析データ")]

    subgraph Fly["Fly.io (nrt) / 内部通信"]
        subgraph Edge["公開入口（Browser から到達可能）"]
            direction LR
            FE["frontend<br/>認証 / proxy / 公開入口"]
            RL[("redis<br/>レート制限")]
            FE --- RL
        end
        subgraph Internal["内部（frontend 経由のみ）"]
            CORE["core<br/>内部API / AI分析 / cron"]
            COLLECT["collect worker<br/>外部ニュース取得"]
            Q[("redis · queue<br/>非同期タスクキュー")]
            CORE --- Q
            COLLECT --- Q
        end
    end

    Browser -->|HTTPS| FE
    FE -->|内部API 呼び出し| CORE
    CORE --> Neon
    COLLECT -->|記事取得| News

    classDef edge fill:#ecfdf5,stroke:#10b981,color:#111827;
    classDef internal fill:#eef2ff,stroke:#6366f1,color:#111827;
    class FE,RL edge
    class CORE,Q,COLLECT internal
```

公開入口、内部 API、外部 HTML 取得 worker、DB 権限を分けることで、外部入力を扱う処理の影響範囲を小さくしています。
詳しい app 分割、DB / Redis / secret の境界、非同期パイプライン、設計判断の背景は [docs/architecture.md](docs/architecture.md) にまとめています。


## ニュース処理パイプライン

収集した記事は、本文補完、翻訳・要約、重要度・投資文脈の分析、ベクトル生成という複数の非同期ステージを通して処理します。各ステージの実行結果は Pipeline Events に記録し、途中で処理が止まった場合は、backfill が DB の状態から未完了の工程を再発見して通常のキューへ再投入します。

この構成を採用した背景や、Redis Streams による再配送、重複実行から DB の整合性を守る仕組みは、Zenn の記事にまとめています。ぜひご覧ください。

[ニュースの収集とAI分析を支える非同期パイプラインの設計]


## Getting Started

ローカルでは Docker Compose で起動できます。Gemini / DeepSeek の API key と、各種 secret の設定が必要です。

```bash
cp .env.example .env
docker compose up -d --build
```

起動後、`http://localhost:3000` を開きます。
環境変数の一覧は [.env.example](.env.example) を参照してください。

## Docs

- [docs/architecture.md](docs/architecture.md): 本番構成、非同期パイプライン、セキュリティ境界、設計判断
- [docs/design-journey/](docs/design-journey/): 設計に対する考え方が変わっていった記録
- [docs/how-i-build-with-ai.md](docs/how-i-build-with-ai.md): AI エージェントとの開発プロセス

## 利用条件

本リポジトリには現時点でオープンソースライセンスを付与していません。
コードの再利用・改変・再配布を希望する場合は、事前に許諾を得てください。
