# VO / Annotated 型レビュー

全モデルのフィールドを「VO (RootModel) / Annotated 型 / 変更不要」に分類し、多層防御を整備する。

## 前提: SQLAlchemy DeclarativeBase 全面移行 (完了)

全モデルの DeclarativeBase 移行は完了済み (`refactor/declarative-base-migration`, PR #18)。
全テーブルが `Mapped[T]` + `mapped_column()` に統一されている。

## 判断基準

| 問い | → |
|---|---|
| イミュータビリティ・等値性が必要？ (dict key / set member) | VO (RootModel + TypeDecorator) |
| 長さ・パターン・空文字列ガードだけ？ | Annotated 型 |
| nullable 自由テキスト / FK / bool / Enum？ | 変更不要 |

## レビュー順序

| 順番 | モデル | ファイル | 状態 |
|---|---|---|---|
| 1 | NewsArticle | [news_article.md](news_article.md) | レビュー済み (2フィールド: ArticleTitle, SafeUrl) |
| 2 | ArticleAnalysis | article_analysis.md | 未着手 |
| 3 | NewsSource | news_source.md | 未着手 |
| 4 | FetchLog | fetch_log.md | 未着手 |
| 5 | WatchlistEntry / AuthRef | — | 変更不要の見込み |

## 完了済みモデル (Phase 1)

- **Category**: CategorySlug, CategoryName (VO) — 実装済み
- **Keyword**: KeywordName (VO) — 実装済み
- **ArticleKeyword**: FK only — 変更不要

## 定義予定の Annotated 型 (`app/domain/types.py`)

| 型名 | 定義 | 使用箇所 |
|---|---|---|
| `ArticleTitle` | `Annotated[str, min_length=1, max_length=500, strip]` | news_article.original_title, article_analysis.translated_title |
| `SafeUrl` | `Annotated[str, min_length=1, max_length=2048, strip, http/https検証]` | news_article.original_url, news_source.site_url/endpoint_url |
| `NonEmptyText` | `Annotated[str, min_length=1, strip]` | article_analysis.summary/reasoning |
| `AiModelName` | `Annotated[str, min_length=1, max_length=100, strip]` | article_analysis.ai_model |

## ブランチ

`refactor/vo-to-annotated-types` (DeclarativeBase 移行完了、Phase 2 から着手)
