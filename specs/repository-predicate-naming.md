# DDD におけるリポジトリ述語メソッドの命名: 実践ガイド

> 作成日: 2026-04-09
> 適用コンテキスト: Vector — WatchlistRepository / ArticleRepository のリファクタリング

## 結論(先に)

ドメイン言語を使え。データベース言語を使うな。そして「全部同じプレフィックスで揃える」というルールに縛られず、各チェックの意味的な性質に応じてプレフィックスを選べ。

Vector のケースに当てはめると:

- `WatchlistRepository.is_watched(user_id, article_id)` — 正しい
- `ArticleRepository.exists_analyzed(article_id)` — これも正しい

両者は **意味的に違う種類の問い** を表現しているので、プレフィックスが揃ってないのは欠陥ではなく **意味的な精度**。

DDD の文献から得られる最強のヒューリスティックは「全 boolean メソッドに同じプレフィックスを付けろ」ではなく、**「ドメインの専門家が質問するときに使う言葉を選べ」**。

## 1. 権威がリポジトリ命名について実際に言っていること

**Eric Evans (DDD 本)** はリポジトリを「オブジェクトのコレクションをエミュレートする、ストレージ・取得・検索を抽象化する仕組み」と定義した。彼の決定的な指示:

> 「ドメインの専門家にとって意味のある基準でオブジェクトを選択するメソッドを提供せよ」

リポジトリのインターフェース言語は **ストレージ技術ではなく、ユビキタス言語に一致すべき**。

**Vaughn Vernon (Implementing DDD)** はこれを補強して、リポジトリと DAO を対比している:

- DAO = データベーステーブルの言葉で表現される。CRUD インターフェースを提供する
- Repository = ユビキタス言語で表現される

Vernon 自身のサンプルコードは `productOfId(tenantId, productId)` や `allProductsOfTenant(tenantId)` のような名前を使っていて、`getById()` や `findAll()` ではない。ドメインの問いを直接表現している。

**Robert Martin (Clean Code)** は「意図を明らかにする名前 (intention-revealing names)」の原則を提示:

> 「変数・関数・クラスの名前は、すべての大きな問いに答えるべきだ。それがなぜ存在するのか、何をするのか、どう使われるのかを伝えるべきだ」

述語(predicate)については、Martin は JavaBeans の慣習に従って `is` プレフィックスを推奨している(例: `paycheck.isPosted()`)。ただし同時にこうも言っている:

> 「1つの抽象概念には1つの単語を選んで貫け」

これがまさに緊張関係の正体 — **プレフィックスの一貫性 vs 意味の正確さ**。

**Martin Fowler (CQS)** はこの議論に Command-Query Separation の視点を加える。存在チェックは純粋な query(副作用のない、bool を返すメソッド)。命名規則は規定しないが、**「これは質問であって命令ではない」と読み手にすぐ分かる名前であるべき** という原則を確立している。

## 2. 3つのプレフィックス: `is_`, `has_`, `exists_`

これらは **交換可能ではない**。それぞれが異なる意味的な関係をエンコードしている。間違ったプレフィックスを選ぶと、読み手の期待とメソッドの実際の動作がズレる。

### `is_` = 状態・性質の述語

主体は既に存在している。問いは「それが特定の状態にあるか?」。

- Python 標準ライブラリ: `str.isalpha()`, `os.path.isfile()`, `inspect.isclass()`
- ドメインモデル: `article.is_published`, `user.is_active`
- 文法構造: 「これは X か?」 — `is` の後に **形容詞 or 過去分詞** が来る

### `has_` = 所有・包含の述語

主体は存在している。問いは「それが何かを持っているか / 含んでいるか?」

- Python: `hasattr()`
- ドメイン語彙: `user.has_permission("edit")`, `order.has_items()`
- 文法構造: 「これは X を持っているか?」

### `exists_` = 存在の述語

問いは「外部のストアやコレクションの中にそれが見つかるか?」

- Python: `os.path.exists()`, Django: `QuerySet.exists()`, Spring Data: `existsById()`
- 文法構造: 「X は存在するか?」 — 動詞は **自動詞(目的語を取らない)**
- **ドメインの問いが「レコードが存在するか」を聞いているとき** に正しいプレフィックス

.NET Framework Design Guidelines のルール:

> 「Boolean プロパティには `Is`, `Can`, `Has` のいずれかをプレフィックスとして付けよ。ただし **価値を加える場合に限って**」

重要なのは、プレフィックスが **文法的かつ意味的に正しい** こと:

- `is_email_exists` — 間違い(文法的に破綻。「email is exists か?」)
- `email_exists` — 正しい
- `is_watched` — 正しい
- 両者は **異なる問題を解決している**

## 3. ドメイン言語 vs 技術言語: 境界線をどこに引くか

Vladimir Khorikov のフレームワーク:

| カテゴリ | 技術用語の許容 | 例 |
|---|---|---|
| コアドメインクラス(エンティティ、VO、ドメインイベント) | すべて排除 | `Article`, `ImpactLevel` |
| インフラクラス(リポジトリ、サービス、アダプタ) | クラス名には許容 | `ArticleRepository` |
| インフラクラスの **メソッドシグネチャ** | ドメイン言語を使うべき | `is_watched()`, NOT `entry_exists()` |

リポジトリの **メソッドシグネチャ自体は ドメイン言語を使うべき**。なぜならそれはドメイン層のインターフェースの一部だから。

### `entry_exists` がアンチパターンである理由

"entry" はデータベース/インフラの用語で、リポジトリのインターフェース越しに漏れ出している。ドメインの専門家は「ウォッチリストエントリは存在するか?」とは言わない。「この記事はウォッチされているか?」と言う。

同様に `row_exists`, `record_found`, `tuple_present` も全部、リポジトリの「ストレージを抽象化する」という核心的な目的に違反している。

### Phil Calcado テスト

リポジトリを Yoshima 命名規則で書き直したら、どちらのメソッド名がまだ意味を成すか?

- `WatchedArticles.is_watched(user_id, article_id)` — 自然に読める
- `WatchedArticles.entry_exists(user_id, article_id)` — 不自然

### トレードオフ

ドメイン特化しすぎた名前は、実際にメソッドが何をやっているか(インフラレベルで)が見えにくくなるという側面もある。`is_watched` が実は `EXISTS (SELECT 1 FROM watchlist WHERE ...)` を発行していると、知らない人はメモリ上のフラグを見ていると誤解するかもしれない。

緩和策: **一貫したドキュメントと一貫したパターン**。リポジトリの `is_xxx` メソッドが全部 DB クエリに委譲しているなら、その規則自体が時間とともに自己説明的になる。

## 4. 予測可能性を最重要の命名基準にする

### 「声に出して読む」テスト

呼び出し箇所が **自然な英文** として読めるなら、名前は良い:

- `if article_repo.exists_analyzed(article_id)` — 「分析済み記事が存在するか」 OK
- `if watchlist_repo.is_watched(user_id, article_id)` — 「記事がウォッチされているか」 OK
- `if watchlist_repo.entry_exists(user_id, article_id)` — 文法は通るがドメイン言語テストに失敗

### 「何を返すか」テスト

bool を返すメソッドは、条件文の中で意味を持つ名前であるべき:

- `if is_watched:` — 自然に読める
- `if entry_exists:` — 「entry」が何を指すか読み手にデコードを強いる

### 「ストレージを変えたら?」テスト

SQL から graph DB や event store に移行したら、メソッド名はまだ意味を成すか?

- `is_watched`, `exists_analyzed` — ストレージに依存しない
- `entry_exists`, `row_found` — ストレージに依存

### CQS シグナル

述語(bool を返すクエリ)は **質問として読める名前** にすべき。`is_` も `exists_` も「これは質問であって動作ではない」とシグナルを送る。

## 5. 2つのリポジトリの非対称性の解決

### ArticleRepository のチェック: コレクション内の存在

- ドメインの問い: 「この記事は分析されているか?」
- 実装の現実: 「`article_analyses` テーブルに行が存在する = 分析済み」
- バイナリ: レコードが存在するか、しないか
- `exists_analyzed` が正しい — 「何かの **存在(presence)** をチェックしている」ことを伝える
- `is_analyzed` にすると「記事は変更可能な `analyzed` 状態を持つエンティティ」という含みが出てしまう

### WatchlistRepository のチェック: 関係性 / 状態の述語

- ドメインの問い: 「この記事はこのユーザーにウォッチされているか?」
- 2つのエンティティ間の関係性に関するドメインの問い
- `is_watched` — これをドメイン概念として伝える
- `entry_exists` — これをデータベース操作として伝える

### DDD 文献はこの非対称性を支持する

Evans, Vernon, Calcado は全員 **意味的な正確さ** を **機械的な一貫性** より優先する。Martin の「1つの抽象概念には1つの単語」は、概念が同じときに適用される。しかし「存在 (existence)」と「状態 (state)」は **同じ概念ではない**。両方を `exists_*` か `is_*` のどちらかに無理矢理揃えると、表面的な一貫性のために意味的な正確さを犠牲にすることになる。

### Vector での適用

```python
# 存在チェック - ドメインの問いは「分析済み記事が存在するか」
ArticleRepository.exists_analyzed(article_id) -> bool

# 関係性の述語 - ドメインの問いは「この記事はウォッチリストにあるか」
WatchlistRepository.is_watched(user_id, article_id) -> bool
```

## 6. コマンドメソッドにも同じ原則を適用する

述語メソッドと同じく、コマンドメソッド(副作用のある操作)もドメイン語彙で命名する。

### `add_entry` / `remove_entry` がアンチパターンである理由

`entry_exists` と同じ罠。"entry" はデータ保存用語であり、ドメイン言語ではない。

- `add_entry` — 「ウォッチリストエントリを作成する」= データ構造の話
- `watch` — 「この記事をウォッチする」= ドメインの話

### ドメイン動詞スタイル: watch / unwatch / is_watched

```python
class WatchlistRepository:
    async def watch(self, user_id: UUID, article_id: int) -> None: ...
    async def unwatch(self, user_id: UUID, article_id: int) -> int: ...
    async def is_watched(self, user_id: UUID, article_id: int) -> bool: ...
```

三位一体の対称性: `watch` / `unwatch` / `is_watched` がドメイン動詞「watch」を中心に揃う。

呼び出し箇所の読みやすさ:

```python
if not await self.watchlist_repo.is_watched(user_id, article_id):
    await self.watchlist_repo.watch(user_id, article_id)
```

英文として完璧に読める。「もしウォッチされていないなら、ウォッチする」。

### Service 層との語彙の分離

Service 層のメソッド名(アプリケーションのユースケース)と Repository 層のメソッド名(ドメインの操作)は異なるが呼応している:

```python
class WatchlistService:
    async def add_to_watchlist(self, ...) -> None:
        # Service = ユースケースを表現 → 説明的
        await self.watchlist_repo.watch(user_id, article_id)
        # Repository = ドメインの操作を表現 → 動詞的
```

層ごとに適切な抽象度の語彙を使う。

## 7. 避けるべきアンチパターン

### データベース / ORM の語彙を漏らす

リポジトリの公開インターフェースに **出してはいけない言葉**:

`row`, `record`, `entry`, `tuple`, `table`, `column`, `session`, `query`

### 一般的な CRUD 動詞を場当たり的に混ぜる

`get`, `find`, `fetch`, `retrieve` を一貫したルールなしに使い分けると認知負荷が増える。

- Cosmic Python: `get()` と `add()` だけ使う
- Vernon: `productOfId()` のようなドメイン特化名

どちらか一方を選ぶ。混ぜない。

### アプリケーションのユースケースで名前を付ける

`get_pending_orders_for_dashboard()` のような名前は **アプリケーションサービスの責務** であってリポジトリの責務ではない。

### クラス名にストレージ実装を含める

`UserSQLRepository`, `MongoArticleRepo` はインターフェースをインフラに結合させる。

## 8. Vector の命名規約

### 述語メソッド

| プレフィックス | 意味 | 使い所 | 例 |
|---|---|---|---|
| `exists_` | コレクションの存在チェック | レコードが存在するかどうか | `exists_analyzed(article_id)` |
| `is_` | ドメイン状態/関係性の述語 | エンティティの状態や関係性 | `is_watched(user_id, article_id)` |
| `has_` | 所有/包含の述語 | 何かを含んでいるか | (今後必要になった時点で適用) |

### コマンドメソッド

ドメイン動詞を使い、ストレージ語彙を避ける:

- `watch()` / `unwatch()` — NOT `add_entry()` / `remove_entry()`
- `fetch_articles()` — コレクション取得は `fetch_` で統一(既存パターン)

### 選択の判断プロセス

1. そのメソッドが答える「ドメインの問い」を一文にしてみる
2. その問いを文法的に分解する — 「存在するか」なのか「状態か」なのか「持っているか」なのか
3. 対応するプレフィックスを選ぶ

## 参考文献

- Eric Evans, *Domain-Driven Design* (2003) — リポジトリの定義とユビキタス言語
- Vaughn Vernon, *Implementing Domain-Driven Design* (2013) — DAO vs Repository の対比
- Robert Martin, *Clean Code* (2008) — 意図を明らかにする名前
- Martin Fowler, *Command-Query Separation* — 副作用のない query の命名
- Vladimir Khorikov — コアドメイン vs インフラの語彙境界
- Phil Calcado, *How to Write a Repository* — Yoshima 命名
- .NET Framework Design Guidelines — Boolean プロパティのプレフィックス規則
