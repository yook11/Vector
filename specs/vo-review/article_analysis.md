# article_analysis.py — VO / Annotated 型レビュー

## 対象モデル

`backend/app/models/article_analysis.py` (DeclarativeBase)

## フィールド一覧と判断

| # | フィールド | 現在の型 | nullable | 判断 | 理由 |
|---|---|---|---|---|---|
| 1 | `id` | `int` | PK | 変更不要 | 自動採番 |
| 2 | `news_article_id` | `int` | NOT NULL, FK, UNIQUE | 変更不要 | FK + UniqueConstraint で保護済み |
| 3 | `translated_title` | `str` (max 500) | NOT NULL | **CHECK 制約追加** | 作成パスが1つ（AI分析パイプライン）。空文字列を防ぐ CHECK のみ |
| 4 | `summary` | `str` (Text) | NOT NULL | **CHECK 制約追加** | 同上。AI生成テキストだが空を許容すべきでない |
| 5 | `reasoning` | `str` (Text) | NOT NULL | **CHECK 制約追加** | 同上。判定の根拠が空では意味がない |
| 7 | `ai_model` | `str` (max 100) | NOT NULL | **CHECK 制約追加** | モデル名が空では監査に使えない |
| 8 | `analyzed_at` | `datetime` (TZ) | NOT NULL | 変更不要 | server_default=func.now() で DB に委譲済み |
| 9 | `embedding` | `Vector(768) \| None` | NULL | 変更不要 | pgvector 専用型。後から非同期で付与される |
| 10 | `embedding_model` | `str \| None` (max 100) | NULL | 変更不要 | embedding と対で付与されるメタデータ |

## 判断基準の適用

### VO が不要な理由

全フィールドの作成パスが **AI 分析パイプライン (`ai_analyzer.analyze_article()`) の1箇所のみ**。
複数文脈で同じバリデーション/正規化が必要になるフィールドが存在しない。

| 基準 | 該当フィールド | → |
|---|---|---|
| 複数箇所で同じバリデーション/正規化が必要 | **なし** | — |
| 単一パスでしか作られない | 全フィールド | VO 不要 |

### Annotated 型も不要な理由

- `translated_title` / `summary` / `reasoning`: AI 生成テキスト。フォーマット制約（文字数パターン等）を課す意味がない
- `ai_model`: 設定値 (`settings.ai_model_name`) からそのまま格納。パターン制約はモデル名の命名規則に依存しすぎる

→ **DB CHECK 制約（空文字列の防止）だけで十分**

## 各フィールドの詳細

### translated_title — CHECK 制約追加

- **現状の問題**: NOT NULL だが空文字列 `""` を通してしまう
- **作成パス**: `ai_analyzer.py:165` — `strip_html_tags()` 適用後に格納。フォールバックが空文字列
- **読み取りパス**: `routers/news.py:81` → `AnalysisResponse.translated_title`
- **対策**: DB CHECK `translated_title != ''`
- **注意**: `strip_html_tags()` が None を返した場合のフォールバック `""` は CHECK 違反になる。パイプライン側でエラーハンドリングを検討する必要あり

### summary — CHECK 制約追加

- **現状の問題**: 同上
- **作成パス**: `ai_analyzer.py:166` — Gemini 応答の `summary_ja` を `strip_html_tags()` 後に格納
- **読み取りパス**: `routers/news.py:82` → `AnalysisResponse.summary`
- **対策**: DB CHECK `summary != ''`

### reasoning — CHECK 制約追加

- **現状の問題**: 同上
- **作成パス**: `ai_analyzer.py:168` — `strip_html_tags()` 適用後に格納
- **読み取りパス**: `routers/news.py:84` → `AnalysisResponse.reasoning`
- **対策**: DB CHECK `reasoning != ''`

### ai_model — CHECK 制約追加

- **現状の問題**: 空文字列が入りうる（実運用では `settings.ai_model_name` が設定されるので可能性は低い）
- **作成パス**: `ai_analyzer.py:169` — `analyzer.model_name` を格納
- **読み取りパス**: `routers/news.py:85` → `AnalysisResponse.ai_model`
- **対策**: DB CHECK `ai_model != ''`

### 変更不要フィールドの理由

- `news_article_id`: FK + UniqueConstraint で 1:1 関係を保護済み
- `analyzed_at`: server_default=func.now() で DB に委譲。アプリ側で設定しない
- `embedding`: pgvector の Vector(768) 型。NULL は「未生成」を意味し正当
- `embedding_model`: embedding と対で付与。NULL は embedding 未生成と同義

## 追加する CHECK 制約

```sql
ALTER TABLE article_analyses ADD CONSTRAINT ck_article_analyses_translated_title_not_empty
  CHECK (translated_title != '');

ALTER TABLE article_analyses ADD CONSTRAINT ck_article_analyses_summary_not_empty
  CHECK (summary != '');

ALTER TABLE article_analyses ADD CONSTRAINT ck_article_analyses_reasoning_not_empty
  CHECK (reasoning != '');

ALTER TABLE article_analyses ADD CONSTRAINT ck_article_analyses_ai_model_not_empty
  CHECK (ai_model != '');
```

## パイプライン側の影響

`ai_analyzer.py` の `strip_html_tags()` フォールバック:

```python
translated_title=strip_html_tags(data.translated_title) or "",  # CHECK 違反の可能性
summary=strip_html_tags(data.summary) or "",                     # CHECK 違反の可能性
reasoning=strip_html_tags(data.reasoning) or "",                 # CHECK 違反の可能性
```

CHECK 制約追加後、`strip_html_tags()` が None/空を返した場合は IntegrityError になる。
対応方針:
- AI が空レスポンスを返すのは異常系 → INSERT を失敗させてログに記録するのが正しい
- `or ""` フォールバックを削除し、空の場合はスキップ or 例外を投げる

## 結論

- **VO 対象**: なし（全フィールドが単一パスで作成）
- **CHECK 制約追加**: `translated_title`, `summary`, `reasoning`, `ai_model` の4フィールド
- **変更不要**: `id`, `news_article_id`, `analyzed_at`, `embedding`, `embedding_model`

> **記録**: 旧 `impact_level`（StrEnum + CHECK 制約）は 2026-04 に完全廃止した。本ドキュメントの行 5 と「impact_level — 変更不要」節は削除済み。
