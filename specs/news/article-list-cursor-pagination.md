# 記事一覧とウォッチリストのカーソル方式

Status: Implemented
工程: backend(schemas/cursor.py ほか) → /gen-types → frontend → /check

## 目的

記事一覧とウォッチリストの位置を「何件目」ではなく「この記事の次」で指すカーソル方式にし、画面は「さらに読み込む」で下に足していく形にする。
新しい記事の分析で一覧の先頭側が変わっても、続きに重複・欠けが出ないようにする。

## API

| | `GET /api/v1/articles` | `GET /api/v1/me/watchlist` |
|---|---|---|
| パラメータ | `category`(任意)、`cursor`(任意) | `cursor`(任意) |
| 並び | 公開日時の新しい順、同時刻は記事 ID の大きい順 | ウォッチした時刻の新しい順、同時刻は記事 ID の大きい順 |
| レスポンス | `AnalyzedArticlePreviewList` | `AnalyzedArticlePreviewList` |
| 認可 | BFF 経由証明 | ログイン |

- `AnalyzedArticlePreviewList` は `items: AnalyzedArticlePreview[]` と `nextCursor: string | null`。`nextCursor` は必須で、null なら続きはない。
- 1回24件 (`ARTICLE_LIST_LIMIT`)。1件多く読んで続きの有無を決め、件数は数えない。
- 廃止: `page` / `perPage` / `sortOrder` パラメータと、`total` / `page` / `perPage` / `totalPages`。廃止したパラメータを付けても無視して 200 を返す。

## カーソル

- 位置 (記事は `{published_at, id}`、ウォッチは `{watched_at, article_id}`) の JSON を base64url (padding なし) にした不透明な文字列。クライアントは中身を解釈せず、受け取った `nextCursor` をそのまま `cursor` に渡す。
- 位置だけを持ち、category・署名・版は入れない。時刻はマイクロ秒とタイムゾーンを保つ。
- 読めない値は 422。base64url 以外の文字、JSON でない、項目の過不足、タイムゾーンのない時刻、記事 ID の上限超え、256字超え、もう一方の一覧のカーソルが該当する。
- 位置型は pydantic dataclass で定義する。BaseModel だと FastAPI が OpenAPI の components に載せ、カーソルの中身が公開型になる。
- 記事一覧は行比較に `published_at <= カーソルの時刻` を併記し、`idx_analyzable_articles_published` をカーソルの位置から読む。ウォッチリスト用の索引は追加しない (1人あたりの件数が少ない)。

## 画面

- ダッシュボードとウォッチリストは最初の24件をサーバーで描き、「さらに読み込む」で Server Action (`loadMoreArticles` / `loadMoreWatchlist`) から続きを取って下に足す。上限はなく、前へ戻る操作はない。
- 失敗したら「読み込めませんでした」と「もう一度読み込む」を出す。1回以上読み込んで最後に達したら「これ以上の記事はありません」を出す。
- 読み込んだ続きは、新しい遷移 (リンク・push / replace) で捨てて先頭から出し、戻る/進む・Server Action 後の再描画では保つ (`useRouter().bfcacheId` を key にする)。ダッシュボードはカテゴリの変更と「一覧を更新」(revision の変化) でも先頭から出す。
- ウォッチ状態は全ウォッチ ID (`getWatchlistIds`) で判定する。ウォッチの操作後にサーバーが取り直すので、足した記事のボタンも正しく切り替わる。ウォッチリストで解除した記事は、読み込み済みの一覧からすぐ外す。
- 一覧の上の帯はカテゴリ名だけを出す (`ArticleListHeading`)。並び順と表示件数の選択は廃止する。
- `@/features/watchlist` はブラウザ側からも読める内容 (`WatchlistButton`・Server Action) だけにし、サーバー専用の取得関数は `@/features/watchlist/server` に分ける。

## 不変条件

- `AnalyzedArticlePreview` の形、`/articles/{id}`・`/similar`・`/me/watchlist/ids` は変えない。
- 続きの取得で重複・欠けがない (同時刻の境目を含む)。ウォッチリストは本人の記事だけを返す。
- 読めないカーソルは 500 にならず 422 を返す。

## 破壊的変更

- 2つの一覧のパラメータとレスポンスの形。frontend と同じ PR で反映し、rollout 中の数分の不整合は許容する。frontend は `nextCursor` の無い応答を「続きなし」として扱う。
- `?page=` / `?perPage=` / `?sortOrder=` の付いた古い URL は、それらを無視して新しい順の先頭を出す。

## 対象外

- リサーチ履歴のページング (`PaginationParams` は research 用に残る)。
- ウォッチ ID を表示中の記事に絞ること (追記型を前提に後で設計する)。
- `@/features/news` など他の feature の入口の分割。
- `article_eager_options_brief` / `article_eager_options_detail` の改名、e2e の追加。

## 受け入れ条件

- [x] 2つの一覧が `category` / `cursor` だけを受け、`AnalyzedArticlePreviewList` を返す (OpenAPI に位置型が出ない)
- [x] 同時刻の境目でも重複・欠けがなく、他ユーザーのウォッチが混ざらず、読めないカーソルは 422 (実 DB 試験)
- [x] 深い位置のカーソルでも索引をカーソルの位置から読む (開発 DB の EXPLAIN)
- [x] 追記・末尾・失敗時の再試行・解除した記事を外す・旧応答を続きなしとして扱う (コンポーネント試験)
- [x] `/check` と `next build` が pass
