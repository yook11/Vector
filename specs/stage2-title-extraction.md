# Stage 2 HTML title 抽出 + Stage 1 責任境界の整理

> ステータス: 設計確定（実装待ち）

## 背景

現状のパイプラインは以下の責務配置になっている:

- **Stage 1 (ingestion)**: RSS/HN の構造化フィードから URL + title を取得し、`discovered_articles` に保存。title が空であれば URL ごと reject (`ArticleCandidate.from_external` が None を返す)
- **Stage 2 (extraction)**: `ArticleHtmlExtractor` が HTML から body + published_at を抽出し、`Article` 行を作成。title は **Stage 1 由来の `discovered.original_title` をそのままコピー**

この設計には責任境界の曖昧さがある:

- **本文は Stage 2 が authoritative** になっている（RSS がフルテキスト content を提供していても Stage 2 で HTML から取り直す、`table-separation.md` パイプライン変更項目参照）
- しかし **title だけは Stage 1 が authoritative**（Stage 2 は受動的にコピーするだけ）
- 本文・title の非対称は「Stage 2 = 分析品質の担保」という責任の表現として歪んでいる

また `persist_new_articles` の dedup は「DB 既存 URL」と「バッチ内処理済み URL」の2つの意味を1つの `existing_urls` set に同居させており、`existing_urls.add(candidate.url)` の後付け処理で読み手が引っかかる。

## 解決する問題

1. Stage 2 に title 抽出を委譲し、「Stage 2 = 分析品質の担保（body + title + published_at）」の責任境界を本文と title で一貫させる
2. `persist_new_articles` の入力型を `dict[SafeUrl, ArticleCandidate]` に変え、**URL の一意性を型レベルで保証**する。`existing_urls` の意味は「DB 既存」のみに純化され、関数内の runtime dedup を排除する

## スコープ

### やる (Phase A)

- `HtmlExtractionResult` に `title: str | None` を追加
- `ArticleHtmlExtractor._extract_from_html` で trafilatura の `result.get("title")` を strip + 500 文字上限で整形
- `ContentFetchService` の quality gate に title 必須を追加（body または title が None なら `skipped`）
- `Article` 作成時 `original_title=extraction.title` に切替（`discovered.original_title` 参照を削除）
- `persist_new_articles` 入力 candidates の dedup を `dict.fromkeys` 相当の前処理に統合
- テスト更新

### やらない (Phase A では見送り)

- **Stage 1 の title 収集廃止**: `ArticleCandidate.title` の削除、`discovered_articles.original_title` カラム drop、fetcher の変更は **Phase A では行わない**
  - 理由: Stage 2 で抽出した title の品質を実データで観測した上で判断するため
  - 現状維持で Stage 1 の title は `discovered_articles` に残り続け、Stage 2 の title と比較可能な状態にする

### Phase B（将来、データを見て判断）

Phase A 運用後に `discovered.original_title` と `article.original_title` を比較して以下の方向のどれかを選ぶ:

- HTML 抽出が十分な品質なら **Stage 1 の title 収集廃止**（責任境界を完全に分離）
- 乖離が目立つなら **Stage 1 title を primary、HTML を fallback** に昇格
- 判断がつかないなら両方残して使い分け

## 設計論点と決定

### 論点 A: Stage 2 の title 取得方法 → `trafilatura` の metadata

`trafilatura.bare_extraction(..., with_metadata=True)` は title を以下の優先順位で抽出する:

1. OGP (`<meta property="og:title">`)
2. Twitter Card
3. JSON-LD structured data
4. `<title>` タグ
5. h1

OGP が優先されるため「記事タイトル | サイト名」形式の装飾は大半のモダンサイトで回避される。`<title>` フォールバック時は装飾が混入する可能性があるが、Phase A で観測対象とする。

### 論点 B: Stage 2 の title が取れなかった時の扱い → `skipped`

現状 body が None なら `skipped` を返しているのと同様、**title が None なら `skipped`** とする。Article 行の存在 = 分析可能（title + body 揃っている）という不変条件を維持する。

### 論点 C: Stage 1 の title 収集は維持 → Phase B で判断

`ArticleCandidate.title` と `discovered_articles.original_title` は現状維持。Stage 2 は `discovered.original_title` を参照せず、自前で抽出した title を `articles.original_title` に書く。

結果として:
- `discovered.original_title` = Stage 1 由来（構造化フィードの title）
- `articles.original_title` = Stage 2 由来（HTML 抽出の title）

両方が DB に残るため、同一 URL で比較可能。観測した上で Phase B に進む。

### 論点 D: `persist_new_articles` の入力型 → `dict[SafeUrl, ArticleCandidate]` で型レベル一意性を保証

現状のシグネチャは `candidates: list[ArticleCandidate]` で、重複は型システムから見えない。関数内で `existing_urls` を通じたランタイム dedup を行っており、「DB 既存」と「バッチ内処理済み」の2つの意味を1つの set に同居させている。

変更後:

```python
async def persist_new_articles(
    session: AsyncSession,
    source: NewsSource,
    candidates: dict[SafeUrl, ArticleCandidate],  # キー一意 = URL 重複なしを型で表現
) -> PersistResult:
    result = PersistResult()
    if not candidates:
        return result

    # DB 既存 URL を取得（existing_urls は「DB 既存」のみを意味する）
    existing_urls: set[SafeUrl] = set()
    urls = list(candidates.keys())
    for i in range(0, len(urls), chunk_size):
        # ... IN クエリで既存を取得

    max_new = settings.max_articles_per_fetch
    for url, candidate in candidates.items():
        if url in existing_urls:
            continue
        if len(result.new_discovered) >= max_new:
            break
        discovered = DiscoveredArticle(
            original_title=candidate.title,
            original_url=url,
            news_source_id=source.id,
        )
        session.add(discovered)
        result.new_discovered.append(discovered)

    return result
```

呼び出し側（fetcher）が dict を組み立てる責任を負う:

```python
# RSS / HN 共通パターン
candidates: dict[SafeUrl, ArticleCandidate] = {}
for entry in raw_entries:
    candidate = ArticleCandidate.from_external(raw_url=..., raw_title=...)
    if candidate is None:
        continue
    candidates.setdefault(candidate.url, candidate)  # 先勝ちで重複排除

return await persist_new_articles(session, source, candidates)
```

効果:

- **型レベル保証**: `dict[SafeUrl, _]` のキー一意性が「URL 重複なし」の不変条件を型に刻む（`feedback_structural_guarantee.md`）
- **`existing_urls` の意味純化**: 「DB 既存」のみを指す。`existing_urls.add(candidate.url)` の後付けが消える
- **責任の所在が明確**: 重複排除は呼び出し側（フィードの性質を知っている層）の責任。persister は受け取った一意な候補を DB と突き合わせるだけ

## 実装ステップ

1. `HtmlExtractionResult` に `title: str | None` フィールド追加
2. `_extract_from_html` で title を抽出（strip_html_tags + 500 文字上限、空なら None）
3. `ContentFetchService` 更新
   - quality gate に title 必須チェックを追加
   - `Article(original_title=extraction.title, ...)` に切替
4. `persist_new_articles` のシグネチャを `candidates: dict[SafeUrl, ArticleCandidate]` に変更し、関数内の runtime dedup を削除
5. 呼び出し側（fetcher）を dict 組み立てに変更
   - `hacker_news.py`: `candidates` を dict で構築し `setdefault` で重複排除
   - `rss/base.py`: 同上
6. テスト更新
   - `test_content_service.py`: `_mock_html_extractor` に title 引数追加、既存ケースに title を与え、title 欠落時の `skipped` ケース追加
   - `test_article_persister.py`: 入力を dict に変更。`test_persist_deduplicates_within_batch` は意味が変わる（型レベルで重複不可になったため、runtime dedup 相当のケースは削除 or 呼び出し側テストへ移す）
   - 必要に応じて extractor のユニットテスト追加
7. `ruff check` + `ruff format --check` + `pytest`

## 検証

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/ -x -q
```

## 影響範囲

| カテゴリ | ファイル | 変更種別 |
|---|---|---|
| app(変更) | `collection/extraction/extractor.py` | `HtmlExtractionResult` 拡張、title 抽出ロジック追加 |
| app(変更) | `collection/extraction/service.py` | quality gate + Article 作成時の title ソース変更 |
| app(変更) | `collection/ingestion/persister.py` | 入力型を `dict[SafeUrl, ArticleCandidate]` に変更、runtime dedup 削除 |
| app(変更) | `collection/ingestion/fetchers/hacker_news.py` | candidates を dict で構築 |
| app(変更) | `collection/ingestion/fetchers/rss/base.py` | candidates を dict で構築 |
| tests(変更) | `test_content_service.py` | title 関連ケース追加・既存 fixture 更新 |
| tests(変更) | `test_article_persister.py` | バッチ内重複排除テストが新実装でも動作することを確認 |
| specs(変更) | `table-separation.md` | Stage 2 が title 抽出を担う旨を追記（Phase B 実施時にさらに更新） |

## 関連する既存方針

- `specs/table-separation.md`: 「本文の authoritative source は Stage 2」という既存の判断との一貫性
- `feedback_verify_before_fallback.md`: 実データで検証してから廃止判断する（Stage 1 title 収集廃止を Phase B に保留する根拠）
- `feedback_structural_guarantee.md`: 不変条件を構造で強制（`articles.original_title` NOT NULL を Stage 2 の quality gate で守る）
- `feedback_no_share_different_problems.md`: 実装が似ていても解いている問題が違うなら共用しない（本文と title は解いている問題が同じ = 分析入力の取得、なので Stage 2 で一貫して扱う）
