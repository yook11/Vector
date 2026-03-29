# SQLModel → DeclarativeBase 全面移行プラン

作成日: 2026-03-29
ブランチ: refactor/declarative-base-migration

## 目的

全モデルを SQLAlchemy DeclarativeBase に統一し、以下を達成する:

1. cross-base 制約（ArticleKeyword ↔ NewsArticle）の解消
2. TypeDecorator + type_annotation_map パターンの全モデル適用基盤
3. SQLModel 依存の除去（metadata 共有は維持）

## 前提

- Category, Keyword, ArticleKeyword は移行済み（3層構成パターン確立済み）
- `Base.metadata = SQLModel.metadata` による metadata 共有は継続
- DB スキーマ変更なし（Alembic マイグレーション不要）
- 移行前後で API レスポンスは変更しない

## 移行順序

依存グラフの葉から順に移行し、各フェーズで relationship() が同一 Base 内に閉じることを保証する。

```
[Phase 1]  NewsSource          ← 依存なし（葉ノード）
[Phase 2]  FetchLog            ← NewsSource のみに依存
[Phase 3]  NewsArticle         ← 中心モデル（最大スコープ）
[Phase 4]  ArticleAnalysis     ← NewsArticle に依存
           WatchlistEntry      ← NewsArticle + auth.user(外部) に依存
[Phase 5]  クリーンアップ       ← cross-base コメント除去・最終検証
```

---

### Phase 1: NewsSource

**対象ファイル:** models/news_source.py
**リスク:** 低

作業内容:
- [ ] SQLModel → Base(DeclarativeBase) に書き換え
- [ ] Field() → mapped_column() に変換
- [ ] Relationship() → relationship() に変換（DeclarativeBase 構文）
- [ ] NewsArticle, FetchLog がまだ SQLModel のため、一時的に relationship を FK only に降格
  - `articles` — 使用箇所なし → 一時コメントアウト
  - `fetch_logs` — 使用箇所なし → 一時コメントアウト
- [ ] ruff check + pytest 通過確認

**一時的な制約:**
- NewsSource → NewsArticle: FK only（Phase 3 で復元）
- NewsSource → FetchLog: FK only（Phase 2 で復元）

---

### Phase 2: FetchLog

**対象ファイル:** models/fetch_log.py
**リスク:** 低

作業内容:
- [ ] SQLModel → Base に書き換え
- [ ] Field() → mapped_column() に変換
- [ ] `FetchLog.source` relationship を DeclarativeBase 構文で定義
- [ ] `NewsSource.fetch_logs` relationship を復元
- [ ] ruff check + pytest 通過確認

**復元される relationship:**
- FetchLog.source ↔ NewsSource.fetch_logs（双方 Base — OK）

---

### Phase 3: NewsArticle

**対象ファイル:** models/news_article.py
**リスク:** 中（中心モデル、selectinload 6箇所に影響）

作業内容:
- [ ] SQLModel → Base に書き換え
- [ ] Field() → mapped_column() に変換
- [ ] `NewsArticle.news_source` relationship を DeclarativeBase 構文で定義
- [ ] `NewsSource.articles` relationship を復元
- [ ] ArticleAnalysis, WatchlistEntry は Phase 4 で直後に移行するため relationship は維持
  - Phase 3 単体ではコミットしない（article_analysis の selectinload が壊れるため）
- [ ] **ArticleKeyword ↔ NewsArticle の cross-base 障壁を解消**
  - ArticleKeyword.news_article relationship を追加
  - NewsArticle.article_keywords relationship を追加
  - `_load_article_keywords()` ヘルパーを selectinload に置き換え可能か検討
- [ ] routers/news.py, routers/me.py の selectinload 構文を確認・調整
- [ ] ruff check + pytest 通過確認

**復元される relationship:**
- NewsArticle.news_source ↔ NewsSource.articles（双方 Base — OK）
- ArticleKeyword.news_article ↔ NewsArticle.article_keywords（双方 Base — OK） ※ 新規追加

**影響を受けるファイル:**
- routers/news.py — selectinload(NewsArticle.article_analysis), selectinload(NewsArticle.news_source)
- routers/me.py — selectinload チェーン

---

### Phase 4: ArticleAnalysis + WatchlistEntry

**対象ファイル:** models/article_analysis.py, models/watchlist_entry.py
**リスク:** 低（2モデルは独立、同時移行可能）

作業内容:
- [ ] ArticleAnalysis: SQLModel → Base に書き換え
- [ ] WatchlistEntry: SQLModel → Base に書き換え
- [ ] 全 relationship を DeclarativeBase 構文で復元
- [ ] `auth.user` への FK は外部スキーマ参照のため relationship() なし（現状維持）
- [ ] routers/news.py, routers/me.py の selectinload 構文を最終確認
- [ ] ruff check + pytest 通過確認

**復元される relationship:**
- NewsArticle.article_analysis ↔ ArticleAnalysis.news_article（双方 Base — OK）
- NewsArticle.watchlist_entries ↔ WatchlistEntry.news_article（双方 Base — OK）

---

### Phase 5: クリーンアップ

**リスク:** 低

作業内容:
- [ ] cross-base 制約に関する一時コメントを除去
- [ ] `_load_article_keywords()` ヘルパーが不要になった場合は削除し selectinload に統一
- [ ] model_rebuild() の不要分を整理
- [ ] `alembic revision --autogenerate -m "verify"` で差分ゼロ確認
- [ ] pytest 全テスト通過確認
- [ ] ruff check + ruff format 通過確認

---

## コミット戦略

| コミット | スコープ | 理由 |
|---|---|---|
| 1 | Phase 1〜5 全て | 逆方向 cross-base 問題により段階コミット不可（下記参照） |

**コミット境界の判断基準:** 各コミット時点でアプリが正常動作すること。

### 段階コミットが不可能な理由

当初 Phase 1+2 と Phase 3+4+5 の 2 コミットを計画したが、Phase 1（NewsSource を DeclarativeBase 化）の時点で **NewsArticle.news_source**（SQLModel → DeclarativeBase）が cross-base で壊れる。この relationship は routers/news.py, routers/me.py の selectinload で 6 箇所使用されており、コメントアウトするとアプリが動作しない。

詳細: `relationship-usage.md` §3「段階移行時に発生する逆方向 cross-base 問題」

## 注意事項

1. **DB スキーマ変更なし** — 全フェーズで Alembic マイグレーション不要
2. **metadata 共有維持** — `Base.metadata = SQLModel.metadata` は変更しない
3. **selectinload 構文** — SQLModel の `Relationship` から DeclarativeBase の `relationship()` に変わっても、selectinload の呼び出し構文自体は同じ
4. **未使用 relationship の扱い** — 現在使われていない relationship も back_populates 整合性のため定義は残す
