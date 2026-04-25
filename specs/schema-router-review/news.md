# News — スキーマ / ルーターレビュー

## 対象ファイル

| レイヤー | ファイル |
|---|---|
| Model | `backend/app/models/news_article.py`, `backend/app/models/article_analysis.py` |
| Schema | `backend/app/schemas/news.py`, `backend/app/schemas/embeds.py` |
| Router | `backend/app/routers/news.py` |
| Frontend | `frontend/src/components/news/NewsCard.tsx`, `frontend/src/components/news/NewsDetail.tsx` |

## 現状のスキーマ

```python
class NewsResponse(_CamelBase):
    id: int
    original_title: str
    original_url: str
    source_name: SourceName
    published_at: datetime | None = None
    created_at: datetime
    original_content: str | None = None
    keywords: list[KeywordEmbed] = []
    analysis: AnalysisEmbed | None = None
    is_watched: bool = False

class AnalysisEmbed(_CamelBase):
    translated_title: str
    summary: str
    reasoning: str
    ai_model: str
    analyzed_at: datetime
```

## 問題点

### 1. スキーマ構造がドメインの主従と逆転している

このアプリの中核は「AI 分析済みニュース」の提供。ユーザーが見るのは翻訳タイトル・要約・影響度であり、原文は参照情報にすぎない。

しかし現状のスキーマは:

- **トップレベル** = 原文情報（`original_title`, `original_url`, `original_content`）
- **サブオブジェクト** = AI 分析（`analysis: AnalysisEmbed | None`）

ストレージの都合（`news_article` テーブルが親、`article_analysis` が子）が API の表面にそのまま漏れ出ている。

### 2. 一覧と詳細で同一レスポンス型を共有している

フロントエンドの利用パターン:

| フィールド | カード (NewsCard) | 詳細 (NewsDetail) |
|---|---|---|
| translated_title | o | o |
| summary | o | o |
| source_name | o | o |
| published_at | o | o |
| keywords | o | o |
| is_watched | o | o |
| original_title | - | o |
| original_url | - | o |
| original_content | - | o |
| reasoning | - | o |
| analyzed_at | - | o |

一覧 API で 12 件取得するたびに `original_content`（記事本文）や `reasoning`（分析の推論過程）など、カード表示で使わない大量のテキストが流れている。

### 3. 未分析記事が一覧に混在する

一覧 API は全記事を返しているため、未分析の記事が含まれる。このアプリの価値は「AI 分析済みニュース」であり、未分析記事を一覧に表示する意味がない。

フロントエンドでは `analysis?.translatedTitle ?? article.originalTitle` というフォールバックで対処しているが、これはアプリの中核機能がオプショナル扱いになっている証拠。

### 4. analysis フィールドが Optional である必要がない

本文がない記事 → AI 分析に回さない → 一覧に出す必要がない。一覧を分析済みのみに絞れば、`translated_title` / `summary` は必ず存在する。これらを required にすることで型が嘘をつかなくなる。

### 5. 表示不要なフィールドの露出

- `ai_model`: 内部メタデータであり、ユーザーに表示していない
- `created_at`: フロントエンドで使用していない（`published_at` を使用）

## 解決策

### NewsBrief — 一覧カード用

```python
class NewsBrief(_CamelBase):
    """GET /api/v1/news — 一覧カード用"""
    id: int
    translated_title: str
    summary: str
    source_name: SourceName
    published_at: datetime | None = None
    keywords: list[KeywordEmbed] = []
    is_watched: bool = False
```

- AI 分析フィールドをトップレベルに昇格、全て **required**
- 一覧で不要な `original_*` / `reasoning` / `analyzed_at` を除外
- `analysis` サブオブジェクトを廃止（フラット化）

### OriginalArticleEmbed — 原文参照情報

```python
class OriginalArticleEmbed(_CamelBase):
    """原文記事の参照情報"""
    title: str
    url: str
    content: str | None = None
```

- 原文の 3 フィールドを「参照情報」としてグループ化
- `content` のみ Optional（本文取得に失敗したケース）

### NewsDetail — 詳細画面用

```python
class NewsDetail(_CamelBase):
    """GET /api/v1/news/{id} — 詳細画面用"""
    id: int
    translated_title: str
    summary: str
    reasoning: str
    analyzed_at: datetime
    source_name: SourceName
    published_at: datetime | None = None
    keywords: list[KeywordEmbed] = []
    is_watched: bool = False
    original: OriginalArticleEmbed
```

- Brief との差分は「詳細でしか使わないもの」のみ: `original`, `reasoning`, `analyzed_at`
- スキーマ構造がドメインの主従を表現する: トップレベル = AI 分析結果、`original` = リファレンス

### AnalysisEmbed — 廃止

フラット化により不要。`ai_model` は API から除外。

### 一覧クエリの変更

`GET /api/v1/news` に `ArticleAnalysis` の INNER JOIN を追加し、分析済み記事のみ返す。

## 除外フィールドの整理

| フィールド | Brief | Detail | 判断理由 |
|---|---|---|---|
| `original_title` | 除外 | `original.title` | 一覧では翻訳タイトルが必ず存在 |
| `original_url` | 除外 | `original.url` | 詳細の原文リンクでのみ使用 |
| `original_content` | 除外 | `original.content` | AI 分析の入力材料。詳細では参考表示 |
| `reasoning` | 除外 | トップレベル | 詳細の AI Analysis セクションでのみ使用 |
| `analyzed_at` | 除外 | トップレベル | 同上 |
| `ai_model` | 除外 | 除外 | 内部メタデータ、表示不要 |
| `created_at` | 除外 | 除外 | フロントエンドで未使用 |

## Brief / Detail を継承しない理由

「カードが本体で詳細がその拡張」という継承（`NewsDetail(NewsBrief)`）はドメインの主従が逆転する。また Optional だらけの全部入り型は「この None は未分析だからか、一覧だからか」が区別できない。Brief と Detail は同一リソースの異なるビューであり、親子関係ではないため、独立した型として定義する。

## 波及先

| ファイル | 変更内容 |
|---|---|
| `schemas/news.py` | `NewsResponse` → `NewsBrief` + `NewsDetail`、`AnalysisEmbed` 参照削除 |
| `schemas/embeds.py` | `AnalysisEmbed` 削除、`OriginalArticleEmbed` 追加 |
| `schemas/__init__.py` | export 更新 |
| `routers/news.py` | 一覧: `NewsBrief` + INNER JOIN、詳細/類似: `NewsDetail`、`_build_news_response` を2関数に分離 |
| `routers/me.py` | Watchlist のレスポンスは別途検討（`WatchlistResponse` は News とは独立） |
| Frontend | `/gen-types` 再実行後、`NewsResponse` → `NewsBrief` / `NewsDetail` に型更新 |
