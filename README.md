# Vector

> 海外テックニュース収集・AI翻訳・投資分析ダッシュボード

次世代コンピューティング、マテリアル・インフォマティクスなど、日本では情報が少ない先端分野の海外ニュースを自動収集し、AI で翻訳・要約・インパクト分析を行う投資ダッシュボードです。

公開URL: [https://vectorbrief.online](https://vectorbrief.online)（招待制）
本サービスは外部のAI APIを利用したポートフォリオ作品のため、デモは招待制で公開しています。ご覧になりたい方は、X（@yook_dev）までお気軽にご連絡ください。

## 画面プレビュー

Vector は、海外の先端テックニュースを自動収集し、AI で日本語に翻訳・要約したうえで、投資判断に必要な要点・背景・トレンドを確認できるダッシュボードです。

![カテゴリ別に収集された海外テックニュースを、日本語の要点付きで一覧できるニュースダッシュボード](docs/assets/readme/01-dashboard.png)

## AIエージェントによるリサーチ機能

内部に蓄積された記事と、外部から取得した記事を横断してリサーチし、質問への回答を生成します。

https://github.com/user-attachments/assets/9b2a6caa-37ae-4382-b3c8-47964ee52cfb

以下の記事は、初期実装時点の設計と、そこから見えた課題をまとめた開発記録です。当時は小規模な招待制運用を前提に、まず機能を成立させることを優先しており、将来のスケールを支える実行制御や責任分離を十分に設計へ反映できていませんでした。その反省から、現在は工程構成、run の実行制御、外部検索経路、LLM 呼び出し基盤を見直しています。記事中の構成は現行実装とは異なり、現行設計については別の記事で紹介する予定です。

- [PerplexityライクなQ&Aエージェントを個人開発アプリに組み込んだ——6つの工程に分けた設計の工夫](https://zenn.dev/yook/articles/qa-agent-six-stage-design)
- [PerplexityライクなQ&Aエージェントを個人開発アプリに組み込んだ——スケール時に実行モデルをどう変えるか](https://zenn.dev/yook/articles/qa-agent-scaling-execution-model)

## 主な画面

| ニュース詳細 | ブリーフィング詳細 |
|---|---|
| ![AI が翻訳・要約した記事詳細画面。要点と背景文脈を確認できる。](docs/assets/readme/02-article-detail.png) | ![週次ブリーフィングの詳細画面。複数記事から生成された市場・技術動向の要約を読める。](docs/assets/readme/05-briefing-detail.png) |
| AI が記事を翻訳・要約し、要点と背景文脈を整理する。 | 複数記事をもとに、週次の市場・技術動向を読み物として整理する。 |


## 開発と設計の記録

Vector では、2026年2月にコードを書き始めてから、アプリケーションが形になっていくまでの過程を記録しています。

最初は AI が生成したコードを十分に理解できないまま承認していました。そこから、技術書を読み、既存実装を見直し、失敗を振り返る中で、設計や開発に対する考え方が少しずつ変わっていきました。

[設計の歩み](docs/design-journey/)には、開発を始めてから2026年7月頃までに、自分の考え方がどう変わっていったのかをまとめています。現在とは異なる考えや進め方も含まれていますが、その時点で何を考え、どのようにアプリケーションと向き合っていたのかを残した記録です。

AI との開発の進め方についても、その後、考え方が大きく変わりました。[AI エージェントとの開発の進め方](docs/how-i-build-with-ai.md)では、これまで試してきた方法と、2026年9月時点で考えていることをまとめています。

- 開発を始めてから7月頃までの設計に対する考え方の変化 → [docs/design-journey/](docs/design-journey/)
- アプリケーションの現在の設計と主要な設計判断 → [docs/architecture.md](docs/architecture.md)
- AI との開発で試してきたことと、9月時点での考え → [docs/how-i-build-with-ai.md](docs/how-i-build-with-ai.md)

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
| 非同期処理 | taskiq (worker / scheduler)・ElastiCache Valkey (queue / レート制限)・Transactional Outbox / Amazon SQS / AWS Lambda（段階移行中） |
| データ | Amazon RDS for PostgreSQL・pgvector (768次元ベクトル検索) |
| AI | Gemini (翻訳・要約・リサーチ計画・回答生成・Embedding)・DeepSeek (重要度・投資文脈分析・検索クエリ生成・根拠精査) |
| 外部検索 | Amazon Bedrock AgentCore Gateway (Web Search) |
| 基盤・可観測性 | AWS ECS Fargate (ap-northeast-1)・Terraform・Docker Compose・Logfire (OpenTelemetry)・GitHub Actions |

## Architecture

Vector は、ブラウザから直接到達できる入口を Next.js BFF に寄せ、backend API と worker 群を内部側に閉じる構成です。
本番環境は AWS (ap-northeast-1) で動作しています。ALB を唯一の公開入口とし、frontend・API・scheduler・各 worker を ECS Fargate の service として分離、データは RDS PostgreSQL と ElastiCache Valkey に置いています。構成は Terraform (`infra/aws/`) で管理しています。

以前は Fly.io と Neon PostgreSQL で運用していましたが、この構成はすでに停止しています。現在の本番インフラの正本は `infra/aws/` の Terraform です。

```mermaid
flowchart TB
    Browser([Browser])
    Internet[("インターネット<br/>ニュースソース / 外部 AI API")]

    subgraph VPC["AWS ap-northeast-1 / VPC"]
        subgraph PubIn["public subnet — 入口"]
            ALB["ALB<br/>唯一の公開入口"]
        end

        subgraph AppNet["app subnet — public IP を持たない"]
            FE["frontend<br/>Next.js BFF / 認証"]
            API["api<br/>FastAPI"]
            WORKER["worker / scheduler<br/>収集・AI分析・派生処理"]
        end

        subgraph DataNet["data subnet"]
            RDS[("RDS PostgreSQL<br/>pgvector")]
            VK[("ElastiCache Valkey<br/>キュー / レート制限")]
        end

        subgraph ProxyNet["proxy subnet"]
            PROXY["egress proxy<br/>許可した宛先だけ通す"]
        end

        subgraph PubOut["public subnet — 出口"]
            NAT["NAT Gateway"]
        end
    end

    Browser ==>|HTTPS| ALB
    ALB ==>|内部へ転送| FE
    FE -->|内部 API 呼び出し| API
    FE ~~~ WORKER
    API --> RDS
    API --> VK
    WORKER --> RDS
    WORKER --> VK
    WORKER ==>|外向き通信| PROXY
    PROXY ==>|許可した宛先だけ| NAT
    NAT ==>|固定 IP で送信| Internet

    linkStyle 0,1 stroke:#10b981,stroke-width:3px
    linkStyle 8,9,10 stroke:#f59e0b,stroke-width:3px

    classDef edge fill:#ecfdf5,stroke:#10b981,color:#111827;
    classDef internal fill:#eef2ff,stroke:#6366f1,color:#111827;
    classDef data fill:#fef3c7,stroke:#f59e0b,color:#111827;
    class ALB,NAT edge
    class FE,API,WORKER,PROXY internal
    class RDS,VK data
```

公開入口、内部 API、外部 HTML 取得 worker、DB 権限を分けることで、外部入力を扱う処理の影響範囲を小さくしています。
この分割の背景と、非同期パイプライン・セキュリティ境界の設計判断は [docs/architecture.md](docs/architecture.md) にまとめています。ただし、同文書のインフラ構成は旧 Fly.io / Neon 運用時の記録であり、現在の AWS 構成を説明するものではありません。

以下の記事は、Fly.io / Neon から AWS へ移行した時点の選定理由とトレードオフをまとめた記録です。現在も本番基盤には AWS を利用していますが、個別の構成や運用方式はその後も更新しており、現行構成の正本は `infra/aws/` です。

[個人開発サービスを Fly.io + Neon から AWS に移行した — 選定の理由とトレードオフ](https://zenn.dev/yook/articles/aws-migration-from-flyio-neon-tradeoffs)


## ニュース処理パイプライン

収集した記事は、本文補完、翻訳・要約、重要度・投資文脈の分析、ベクトル生成という複数の非同期ステージを通して処理します。各ステージの実行結果は Pipeline Events に記録し、途中で処理が止まった場合は、backfill が DB の状態から未完了の工程を再発見して通常のキューへ再投入します。

現在はコスト最適化のため、常時稼働 worker と taskiq / Valkey を中心とした構成から、Transactional Outbox・Amazon SQS・AWS Lambda を利用するイベント駆動構成へ段階的に移行しています。

以下の記事は、移行前の Redis Streams を中心とした非同期パイプラインについて、再配送や重複実行から DB の整合性を守る仕組みをまとめた開発記録です。

[ニュースの収集とAI分析を支える非同期パイプラインの設計](https://zenn.dev/yook/articles/redis-streams-async-pipeline-recovery)


## Getting Started

ローカルでは Docker Compose で起動できます。Gemini / DeepSeek の API key と、各種 secret の設定が必要です。

```bash
cp .env.example .env
docker compose up -d --build
```

起動後、`http://localhost:3000` を開きます。
環境変数の一覧は [.env.example](.env.example) を参照してください。

## Docs

- [docs/architecture.md](docs/architecture.md): アプリケーション設計、非同期パイプライン、セキュリティ境界、設計判断（インフラ構成は Fly.io 運用時の記述）
- [docs/design-journey/](docs/design-journey/): 設計に対する考え方が変わっていった記録
- [docs/how-i-build-with-ai.md](docs/how-i-build-with-ai.md): AI エージェントとの開発プロセス

## 利用条件

本リポジトリには現時点でオープンソースライセンスを付与していません。
コードの再利用・改変・再配布を希望する場合は、事前に許諾を得てください。
