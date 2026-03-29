# news_article.py — VO / Annotated 型レビュー

## 対象モデル

`backend/app/models/news_article.py` (DeclarativeBase)

## フィールド一覧と判断

| # | フィールド | 現在の型 | nullable | 判断 | 理由 |
|---|---|---|---|---|---|
| 1 | `id` | `int \| None` | PK | 変更不要 | 自動採番 |
| 2 | `original_title` | `str` (max 500) | NOT NULL | **Annotated → ArticleTitle** | 空文字列ガードが必要。イミュータビリティ不要 |
| 3 | `original_url` | `str` (max 2048) | NOT NULL, UNIQUE | **Annotated → SafeUrl** | http/https スキーム検証 + 空文字列ガード。等値性不要（UNIQUE制約はDB側） |
| 4 | `original_content` | `str \| None` | NULL | 変更不要 | nullable 自由テキスト（フェッチ結果をそのまま格納） |
| 5 | `original_description` | `str \| None` (max 2000) | NULL | 変更不要 | nullable 自由テキスト（RSS description） |
| 6 | `news_source_id` | `int` | NOT NULL, FK | 変更不要 | FK制約で保護済み |
| 7 | `published_at` | `datetime \| None` | NULL | 変更不要 | ソース依存、null許容が正しい |
| 8 | `created_at` | `datetime \| None` | NOT NULL | 変更不要 | server_default=func.now() 済み |
| 9 | `skip_content_fetch` | `bool` | NOT NULL | 変更不要 | 単純フラグ |

## 判断の詳細

### original_title → ArticleTitle

- **現状の問題**: `NOT NULL` だが空文字列 `""` を通してしまう
- **バリデーション要件**: `min_length=1`, `max_length=500`, `strip_whitespace=True`
- **VO は不要**: dict key / set member として使わない。イミュータビリティの意味がない
- **DB CHECK 制約**: `ck_news_articles_title_not_empty` (`original_title != ''`)

### original_url → SafeUrl

- **現状の問題**: URL形式の検証なし。`javascript:alert(1)` でも通る
- **バリデーション要件**: `min_length=1`, `max_length=2048`, `strip_whitespace=True`, `http/https スキーム検証`
- **VO は不要**: UNIQUE 制約は DB 側。等値性比較は SQL の `=` で行う
- **既存資産の再利用**: `app/utils/sanitize.validate_url_scheme` をそのまま使える
- **DB CHECK 制約**: `ck_news_articles_url_scheme` (`original_url ~ '^https?://.+'`)

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

2フィールドのみ Annotated 型を適用。VO は不要。
