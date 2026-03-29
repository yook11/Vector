# news_article.py — VO / Annotated 型レビュー

## 対象モデル

`backend/app/models/news_article.py` (DeclarativeBase)

## フィールド一覧と判断

| # | フィールド | 現在の型 | nullable | 判断 | 理由 |
|---|---|---|---|---|---|
| 1 | `id` | `int` | PK | 変更不要 | 自動採番 |
| 2 | `original_title` | `str` (max 500) | NOT NULL | **Annotated 型** | 作成パスが1つ（パイプライン）。アプリ層 + DB CHECK で十分 |
| 3 | `original_url` | `str` (max 2048) | NOT NULL, UNIQUE | **VO (SafeUrl)** | 複数文脈で使用（重複チェック・取得・表示）。正規化をVOに閉じ込める |
| 4 | `original_content` | `str \| None` | NULL | 変更不要 | nullable 自由テキスト（フェッチ結果をそのまま格納） |
| 5 | `original_description` | `str \| None` (max 2000) | NULL | 変更不要 | nullable 自由テキスト（RSS description） |
| 6 | `news_source_id` | `int` | NOT NULL, FK | 変更不要 | FK制約で保護済み |
| 7 | `published_at` | `datetime \| None` | NULL | 変更不要 | ソース依存、null許容が正しい |
| 8 | `created_at` | `datetime` | NOT NULL | 変更不要 | server_default=func.now() 済み |
| 9 | `skip_content_fetch` | `bool` | NOT NULL | 変更不要 | 単純フラグ |

## 判断基準: VO vs Annotated 型

| 基準 | → |
|---|---|
| 複数箇所で同じバリデーション/正規化が必要 | **VO** (RootModel + TypeDecorator) |
| 単一パスでしか作られない | **Annotated 型** (アプリ層 + DB制約) |

## 判断の詳細

### original_url → VO (SafeUrl)

- **現状の問題**: URL形式の検証なし。`javascript:alert(1)` でも通る
- **VOが妥当な理由**:
  - 重複チェック（UNIQUE）、コンテンツ取得、表示と**複数の文脈で使用**される
  - スキーム検証・正規化（末尾スラッシュ等）をVO内に閉じ込められる
  - 等値性が意味を持つ（URLの一致判定）
- **実装方針**:
  - `app/domain/safe_url.py` に `SafeUrl(RootModel[str])` を定義
  - URL検証は Pydantic v2 の URL バリデーションを活用（自前 `urlparse` より堅牢）
  - `app/models/types.py` に `SafeUrlType(TypeDecorator)` を追加
  - `base.py` の `type_annotation_map` に登録
- **DB CHECK 制約**: `ck_news_articles_url_scheme` (`original_url ~ '^https?://.+'`)

### original_title → Annotated 型 (ArticleTitle)

- **現状の問題**: `NOT NULL` だが空文字列 `""` を通してしまう
- **Annotated 型で十分な理由**:
  - 記事作成はパイプライン（フィード取得→パース→保存）の**1パスだけ**
  - バリデーションが散らばるリスクがない
  - `String(500)` が最後の安全網として機能
- **実装方針**:
  - `app/domain/types.py` に `ArticleTitle = Annotated[str, StringConstraints(...)]` を定義
  - パイプラインのパース段階で検証（アプリケーション層）
- **DB CHECK 制約**: `ck_news_articles_title_not_empty` (`original_title != ''`)

### 変更不要フィールドの理由

- `original_content` / `original_description`: 外部ソースからの取得データ。null 許容で制約を課す意味がない
- `news_source_id`: FK + ondelete=RESTRICT で保護済み
- `published_at`: ソースによって存在しない場合がある
- `created_at`: server_default で DB に委譲済み
- `skip_content_fetch`: boolean フラグ

## 追加する CHECK 制約

```sql
-- 空文字列を防ぐ
ALTER TABLE news_articles ADD CONSTRAINT ck_news_articles_title_not_empty
  CHECK (original_title != '');

-- URL スキーム検証（DB 第二防御線）
ALTER TABLE news_articles ADD CONSTRAINT ck_news_articles_url_scheme
  CHECK (original_url ~ '^https?://.+');
```

## 結論

- `original_url`: VO (SafeUrl) — 複数文脈で使用、正規化をVOに閉じ込める
- `original_title`: Annotated 型 — 単一パス、アプリ層 + DB CHECK で十分
- 他フィールド: 変更不要
