# ドメインモジュール再設計

> 作成日: 2026-04-15
> ステータス: 設計中（analysis 内部構造確定、collection/search 未確定）

## 背景

### 問題

技術軸（`services/` `repositories/` `ai/` `tasks/`）で水平に切った結果、以下の問題が発生している:

1. **ドメインの繋がりが消える** — 分析・埋め込み・検索は1つのドメインの異なる側面なのに、コード上でその関係が見えない。1つの業務フローを理解するために4つ以上のディレクトリを渡り歩く必要がある

2. **ビジネス重要度とコード構造が逆転** — CRUDが `services/` `repositories/` に整然と並び「本体」に見える。一方、アプリの価値の源泉である知的処理は `tasks/` の中で taskiq デコレータとリトライ設定に囲まれ「ただのバックグラウンド処理」に見える

3. **変更影響の追跡が困難** — AIモデル差し替え（確定している）の影響範囲が `ai/` `services/` `tasks/` `infra/` に散在する

### 解決方針

ドメインプロセスを第一の分類軸とし、技術的関心事はドメインの内部または外部インフラ層に配置する。

## 3つのドメインプロセス

| プロセス | 責務 | 含まれる操作 |
|---------|------|-------------|
| **収集** | 必要な記事を集める | メタデータ取得（RSS/HN/Alpha Vantage）、本文抽出 |
| **分析** | 記事の意味を理解する | AI分析（要約・翻訳・キーワード・インパクト判定）、埋め込み生成 |
| **検索** | 分析結果を使って投資判断に答える | セマンティック検索、将来: グラフ検索、投資意図理解 |

### なぜこの3つか

- **収集と分析は別プロセス**: 収集は外部ソースからデータを持ってくる。分析は持ってきたデータに意味を与える。変更理由が異なる
- **埋め込みは分析の一部**: 要約=意味をテキストで表現、翻訳=意味を日本語で表現、埋め込み=意味をベクトルで表現。すべて「記事の意味を理解し表現する」の異なる側面
- **検索は分析の消費者**: 分析が生成した表現（テキスト、ベクトル、将来のグラフ）を使って問いに答える。分析とは独立して進化する（投資意図理解、グラフ検索等）

## 全体構造

```
app/
  # ── ドメインプロセス ──
  collection/                # 収集（内部構造は未確定）
  analysis/                  # 分析（内部構造確定、下記参照）
  search/                    # 検索（内部構造は未確定）

  # ── CRUD（既存3層、変更なし） ──
  routers/
    articles.py
    watchlist.py
    categories.py
    admin/
      news_sources.py
      pipeline.py
  services/
    articles.py
    watchlist.py
    category.py
    news_source.py
  repositories/
    articles.py
    watchlist.py
    category.py
    news_source.py

  # ── インフラ（横断的関心事） ──
  tasks/                     # taskiq ラッパー（薄い）
    brokers.py
    collection_tasks.py      # 収集ドメインのタスクラップ
    analysis_tasks.py        # 分析ドメインのタスクラップ
  infra/redis/
  models/
  schemas/
  domain/
  config.py, db.py, dependencies.py, main.py
```

### 設計判断

| 判断 | 理由 |
|------|------|
| `tasks/` はドメインの外に残す | taskiq デコレータ・リトライ・ブローカー選択はインフラ関心事。ドメインパッケージは純粋なドメイン操作を公開し、tasks/ がラップする |
| `models/` `schemas/` は共有のまま | ArticleAnalysis は分析が書き検索が読む。パッケージに閉じ込めると逆に依存が複雑化 |
| `search/` は router まで含む | 検索はドメインとして完結しており、CRUD 側の routers/ に混ぜる理由がない |
| CRUD 側は変更しない | 3層で問題なく機能している。手を入れるメリットがない |

## analysis パッケージ内部構造（確定）

```
analysis/
  service.py                 # analyze_article() — AI分析+埋め込みを段取る
  analyzer/
    base.py                  # BaseAnalyzer — 抽象+リトライエンジン
    gemini.py                # GeminiAnalyzer — Gemini固有のプロンプト・パース
  embedder/
    base.py                  # BaseEmbedder — 抽象+リトライエンジン
    gemini.py                # GeminiEmbedder — Gemini埋め込みAPI
  errors.py                  # 分析ドメイン全体のエラー階層
  dedup.py                   # 重複検出
```

### 層の構成

- **service.py** = ドメイン層。「記事を分析するとは何をすることか」を定義
- **analyzer/ embedder/** = 技術層。「Gemini をどう叩くか」の実装詳細

### service.py のインターフェース

```python
class AnalysisService:
    async def analyze(self, session, article_id: int) -> None:
        """AI分析を実行して保存（冪等）"""
        ...

    async def embed(self, session, article_id: int) -> None:
        """分析結果を入力に埋め込みを生成・保存（冪等）。
        分析済みが前提条件（ガードで保護）。"""
        analysis = await self._load(session, article_id)
        if not analysis.has_ai_analysis:
            raise AnalysisDomainError("Cannot embed: AI analysis not completed")
        ...
```

### tasks/ との連携

```python
# tasks/analysis_tasks.py
@broker_analysis.task
async def analyze_article_task(article_id: int):
    await analysis_service.analyze(session, article_id)
    await generate_embedding_task.kiq(article_id)  # 配線

@broker_embedding.task
async def generate_embedding_task(article_id: int):
    await analysis_service.embed(session, article_id)
```

- **service は2メソッド公開**: `analyze()` + `embed()`。各々冪等
- **ドメイン契約は service 内**: `embed()` が分析済みをガード。順序を間違えても壊れない
- **順序の配線は tasks/**: `analyze -> embed` のチェーンは tasks/ が持つ
- **キューは2つ維持**: AI分析（重い・長い）と埋め込み（軽い・短い）は実行特性が異なるため別ブローカー
- **2分割**: AI分析（analyzer/）と埋め込み（embedder/）で確定。将来のプロンプト分割（Phase 2）で analyzer/ を再分解

### 埋め込みの入力

埋め込みは**分析結果（日本語要約等）をベクトル化**する方針（確定）。現状は元記事テキストだが変更予定。
分析->埋め込みの順序にはデータ依存があるが、service のガードで保護されるため、順序知識を service 内に閉じ込める必要はない。

### 2分割の根拠

| 分割単位 | 独立性の根拠 |
|---------|------------|
| AI分析 | 要約・翻訳・キーワード・インパクト判定は現状1プロンプトで一体。将来分割する蓋然性はあるが、具体的なモデル選定・実行順序が未定 |
| 埋め込み | 別モデル・別API。モデル変更確定。変更理由が AI分析と完全に独立 |

将来「キーワード検出を別モデル化」「埋め込み結果を使ってインパクト判定」等の変更が具体化した時点で、analyzer/ を operations/ に分解する（Phase 2）。

## 今後のロードマップ

| 項目 | ステータス |
|------|----------|
| ベクトルモデル変更 | 確定。analysis/embedder/ の差し替えで対応 |
| 投資意図を理解する検索 | 方向性確定。search/ パッケージで進化 |
| グラフ導入 | 方向性確定。analysis/ 内に新たな表現手段として追加 |
| プロンプト分割 | 方向性あり。具体化した時点で Phase 2 として実施 |

## 設計原則（このリファクタリングを通じて確立）

1. **ドメインプロセスでモジュールを切る** — 技術的関心事（API呼び出し/永続化/キュー）ではなく、ドメインの意味（収集/分析/検索）で境界を引く

2. **コード構造はビジネス重要度を表現する** — アプリの価値の源泉がトップレベルのパッケージとして可視化される。インフラに埋もれさせない

3. **現状追従も将来先取りもしない** — 今の実装構造を根拠にせず、かつ具体像が見えていない将来構造も先取りしない。「変更の蓋然性が具体的に見えているか」で判断する
