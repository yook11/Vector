[← 目次](README.md) ・ 次: [第2幕](02-value-objects.md)

# 第1幕 — レビューを始める

「自分でコードを理解できるようになりたい」と思い、最初に始めたのがこのレビューでした。

当時はまだ、ユーザーからリクエストが来て、どのようにデータが返っていくのか？その全体像すら掴めずにいました。

コードベースをみてもどこで何をしているのか全くわからないところからのスタートでした。

当時、自分が一番理解できる可能性がある部分はどこなんだろう？と考えて、『達人に学ぶ DB設計 徹底指南書』と『SQLアンチパターン』の 2 冊を読んでいた経験から、データベース、とりわけ ORM の部分から見ていこうと決めました。

## 1.1 まず、モデルと DB から

最初に、現状を棚卸ししてみることにしました。Claude Code に現在のテーブル設計を書き出してもらい、それを一つひとつ見ていくところから始めました。

設計の良し悪しを判断する以前に、自分は ORM、つまり Python 側の models と、実際のデータベースが別物なのだと分かっていなかった、ということに気づきました。

models を書き換えれば、そのままデータベースも変わる。そう思い込んでいました。実際には、models は「データベースはこうあるべき」という宣言にすぎず、現状との差分を実際のデータベースへ反映するのは Alembic の migration でした。レビューを始めるまで、そうしたことを何ひとつ意識していませんでした。

ただ、本当に向き合うことになったのは、知識不足そのものではありませんでした。

棚卸しした内容を確認していく中で、このテーブル構成は自分が設計したものではない、と気づいたのです。

例えば、ニュースソースのテーブルでは、登録した RSS を取得元として管理するだけのはずが、次のような列が並んでいました。

```
+------------------------+--------------------------------------+
| column                 | meaning                              |
+------------------------+--------------------------------------+
| id                     | ソースID                             |
| name                   | ソース名                             |
| source_type            | rss / api                            |
| site_url               | サイトURL                            |
| is_active              | 有効フラグ                           |
| fetch_interval_minutes | 取得間隔（分）      ← 誰も設定しない  |
| next_fetch_at          | 次回取得予定時刻    ← 使われない      |
| last_fetched_at        | 最終取得時刻        ← 使われない      |
| consecutive_errors     | 連続エラー回数      ← 集計されない    |
| last_error_message     | 最終エラー内容      ← 読まれない      |
| feed_url               | RSS用URL（API時NULL）                 |
| etag                   | HTTPキャッシュ用    ← 実装されていない |
| last_modified_header   | HTTPキャッシュ用    ← 実装されていない |
| api_endpoint           | API用（RSS時NULL）                    |
| created_at             | 作成時刻                             |
| updated_at             | 更新時刻                             |
+------------------------+--------------------------------------+
```

その多くは、一度も使われていませんでした。

ここで思い知らされました。コーディングエージェントを使えば、提案を承認していくだけで、それらしいものが出来上がっていきます。
それまでは AI が主導し、自分はその出力を承認してきただけでした。提示された形を受け入れ、並べられた選択肢から選んでいただけで、「自分はこのアプリケーションのデータをどのように扱うべきなのか」を一度も考えていなかったのです。

この気づきが、出発点でした。ここから初めて、自分の意図で一つずつ決め直していくことになります。


## 1.2 「結局ユーザーは何を見たいのか」

API が返すデータをどう直すべきか、最初は正しい設計が何かも、どう見ていけばいいのかさえ分かりませんでした。

とりあえず手を動かそうと、DB のときと同じく、まず技術書に頼りました。『API デザインパターン』(まだ全ては読めていません)を読み始め、あわせて設計のベストプラクティスを AI に調べてもらうようにもなりました。「命名はシンプルに」「前置詞は危険信号」「用途が名前から読み取れるように」——知識は少しずつ入りました。

ただ、それで進められたのは、命名の方向性を揃えることくらいでした。肝心の部分には、どう手をつければいいのか分からないままでした。

知識は入れた。それなのに、コードを読んでも「ここで定義している型が、何に使われているのか、正直まったく分からない」。エンドポイントを眺めて壁打ちするだけでは、答えが出ませんでした。

そんな中、ふと考えが浮かびました。「実際にそのデータが使われている箇所から逆算して確認してみるのがいいのでは？」
本当に欲しい情報は何か。結局ユーザーは何を見たいのか。その視点から見直してみることにしました。

その目で見ると、問題点が見えるようになってきました。

このアプリの中核は「AI 分析済みニュース」なのに、レスポンスは取得したニュースの原文が主役になっていました。型の名前は NewsResponse——「返すのはニュースだ」という前提で、最上位に原文タイトルや元記事の URL が並び、AI 分析はその中の省略可能な一フィールドとしてぶら下がっている。「原文ニュースに AI 分析が付いているかもしれない」という形になっていました。

```python
class NewsResponse(_CamelBase):
    # 型の主語は「ニュース」。原文記事がレスポンスの中心に置かれていた。
    id: int
    original_title: str
    original_url: str
    source_name: SourceName
    published_at: datetime | None = None
    created_at: datetime
    original_content: str | None = None
    keywords: list[KeywordEmbed] = []

    # 画面の主役だった AI 分析は、省略可能な付属情報としてぶら下がっていた。
    analysis: AnalysisEmbed | None = None

    is_watched: bool = False


class AnalysisEmbed(_CamelBase):
    # ユーザーが実際に読んでいた見出し・要約・評価は、ここに閉じ込められていた。
    translated_title: str
    summary: str
    impact_level: ImpactLevel
    reasoning: str
    ai_model: str
    analyzed_at: datetime
```

ところが実際の画面でユーザーが見ている見出しも本文も、主役は分析でした。型の上では原文ニュースが主役で、分析が付属している——主従が逆さまだったのです。

さらに、一覧画面と詳細画面が同じ型を共有していて、一覧カードでは表示しないデータを毎回返していました。
返すデータの形を決めるときに、それが画面でどう使われるのかまで考えが及んでいませんでした。

そこで、API で返すデータを、ユーザーが実際に見るものに合わせて、AI 分析を主軸に設計し直しました。さらに、一覧と詳細では必要な情報が違うため、用途に合わせて型を分け、不要なデータを返さない形にしました。

```python
class NewsBrief(_CamelBase):
    # 一覧カードでユーザーが読む情報を、レスポンスの主役にした。
    id: int
    translated_title: str
    summary: str
    impact_level: ImpactLevel

    # 原文由来の情報は、補助的な表示情報として残した。
    source_name: SourceName
    published_at: datetime | None = None
    keywords: list[KeywordEmbed] = []
    is_watched: bool = False


class NewsDetail(_CamelBase):
    # 詳細画面でも、主役は AI 分析済みの内容。
    id: int
    translated_title: str
    summary: str
    impact_level: ImpactLevel
    reasoning: str
    analyzed_at: datetime

    source_name: SourceName
    published_at: datetime | None = None
    keywords: list[KeywordEmbed] = []
    is_watched: bool = False

    # 原文記事は、必要なときに参照する補助情報として分離した。
    original: OriginalArticleEmbed


class OriginalArticleEmbed(_CamelBase):
    title: str
    url: SafeUrl
    content: str | None = None
```

「実際に使われている部分から逆算する」という考え方に切り替えたことで、何を直すべきかが少しずつ見えるようになりました。この経験は、その後のレビューでも判断の軸になっていきます。

## 1.3 エンドポイントレビュー

エンドポイントの層に移ると、これまで見てきたどの層よりも強い違和感がありました。エンドポイントが叩かれたときの処理も、DB クエリの組み立ても、データの変換も、レスポンスの組み立ても、すべてが一つのハンドラに詰め込まれていたのです。

この時はプロジェクトのデフォルトのフォルダ設定で、とても読みにくいものになっていたのですが、どう直すべきかが分からず、FastAPI のレイヤー分離とファイル構成のベストプラクティスをリサーチしてみることにしました。

そこで出てきた記事で、Router / Service / Repository に役割で分けることを知ります。
エンドポイントは router、ビジネスロジックは Service、DB アクセスは Repository。フォルダで役割を分けるという考えが生まれたのは、このときです。

整理を行ったことで、今度はエンドポイントそのものの粗さが見えてきました。

最初に引っかかったのが `/news` です。

```python
router = APIRouter(prefix="/api/v1/news", tags=["news"])

# ユーザーが分析済みニュースの一覧を読むための API
@router.get("", response_model=PaginatedNewsResponse)
async def list_news(...):
    ...

# ユーザーが分析済みニュースの詳細を読むための API
@router.get("/{news_id}", response_model=NewsResponse)
async def get_news(...):
    ...

# ユーザーが関連記事を探すための API
@router.get("/{news_id}/similar", response_model=list[NewsResponse])
async def get_similar_news(...):
    ...


# 管理者がニュース取得ジョブを起動するための API
@router.post("/fetch", response_model=NewsFetchResponse)
async def fetch_news(...):
    ...

# 管理者が未処理の記事に embedding を付与するための API
@router.post("/embed", response_model=EmbedResponse)
async def embed_news(...):
    ...
```

コードだけを見ると、どれも `/news` に関係する API のように見えます。
しかし実際には、ユーザーが記事を読むための API と、管理者が裏側の処理を動かすための API が同じ場所に並んでいました。
そのため、`/news` が「画面に表示する分析済みニュース」を指しているのか、「ニュース取得や AI 処理の入口」を指しているのかが曖昧になり、API の目的が見えにくくなっていました。

そこで、境界を「ニュースというデータ名」ではなく「誰が何のために使うのか」で切り直しました。
ユーザーが扱うものは `/articles`、管理者が操作する処理工程は `/pipeline`。さきほど切り出した `Service / Repository` もこの境界に合わせて下ろし、記事参照とパイプライン操作を別のファイルに分けました。


### 使用されていないエンドポイント

次に、各エンドポイントを確認していくと、どこからも呼ばれていないものがいくつも見つかりました。
特に `keywords` の CRUD は、必要だから設計したのではなく、いつか使うかもしれないものとして残っていた機能でした。
ここでも、自分が考えて設計していなかったことを痛感しました。使われていないものは負債になるので、削除しました。

AI のベクトルを生成するエンドポイントも、どこからも呼ばれていない未使用のエンドポイントでした。ただ、生成に失敗した記事を手動で生成し直す場面があるかもしれない、と考えて残しました。

いま振り返ると、この判断にはまだ「必要になったときに設計する」ではなく、「使うかもしれないから残す」という迷いが残っていました。未使用コードを削るという原則に気づき始めてはいたものの、それを最後まで貫けるほどには、まだ判断軸が固まっていなかったのだと思います。


## 1.4 コードの置き場所

router / service / repository に整理したことで、エンドポイントが叩かれてから下の層へどう流れるかを、追えるようになりました。そこで、エンドポイントを一本ずつ、「このエンドポイントは何をすべきか」から確認し直していきました。

たとえば当時の記事一覧 API は、次のような URL で呼ばれる想定でした。

`GET /api/v1/articles?q=quantum&category=ai&impactLevel=medium&sortBy=publishedAt&sortOrder=desc&page=1&perPage=12`

しかし、このうち `q` は router の関数引数で受け取り、`category` や `impactLevel`、`sortBy`、`perPage` は `ArticleListParams` 側で受け取っていました。
同じ URL の query parameter なのに、コード上では入口が二つに分かれていたのです。

```python
# router 側
@router.get("", response_model=PaginatedArticleResponse)
async def list_articles(
    q: str | None = Query(None, min_length=1, max_length=500),
    ...
    params: ArticleListParams = Depends(),
):
    return await service.list_articles(params, q, ...)

# repositories/articles.py

class ArticleListParams:
    def __init__(
        self,
        keyword_id: Annotated[int | None, Query(alias="keywordId")] = None,
        category: Annotated[str | None, Query()] = None,
        impact_level: Annotated[ImpactLevel | None, Query(alias="impactLevel")] = None,
        per_page: Annotated[int, Query(ge=1, le=100, alias="perPage")] = 12,
    ) -> None:
        ...
```

一箇所にまとめようと中身を開いてみると、その `ArticleListParams` が置かれていたのは、repository でした。
実際にユーザーが投げる URL クエリパラメーターの形まで、DB アクセスのための層に漏れ出していました。

ここで学んだのは、コードを分かりやすくするには、ただ処理を一箇所にまとめればよいわけではなく、
大事なのは、その処理がどの層の責任なのかを考え、置く場所を決めることでした。

そこで、HTTP から受け取る入力と、下の層が使う検索条件を別の型に分け、変換を入口の router に置きました。

```python
class ArticleListParams(BaseModel):
    # HTTP から受け取る生の query params
    category: Annotated[str | None, Query()] = None
    per_page: Annotated[int, Query(alias="perPage")] = 12


@dataclass(frozen=True)
class ArticleListQuery:
    # service / repository が使う、解決済みの検索条件
    category_slug: CategorySlug | None = None
    per_page: int = 12

# そして router で変換する。
def _resolve_params(params: ArticleListParams) -> ArticleListQuery:
    return ArticleListQuery(
        category_slug=CategorySlug(params.category) if params.category else None,
        per_page=params.per_page,
    )
```


さきほどの `ArticleListParams` には、もう一つ隠れた問題がありました。不正なカテゴリを受け取ると、その場で 422 を返していたのです。エラーを HTTP のステータスに変換する処理まで、DB アクセスの層に紛れ込んでいました。

同じ変換は、記事が見つからないときの 404 など、ほかのエンドポイントにも手書きで散らばっていました。どのルーターにも、同じ try/except が並んでいます。

```python
# 各ルーターに同じ変換が並んでいた
@router.get("/{id}")
async def get_article(id: int, service: ArticleService = Depends(...)):
    try:
        return await service.get_article(id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="...")
```

これを、どのエラーがどの HTTP ステータスになるのかを、アプリで一箇所にまとめて登録する形にしました。サービスは例外を raise するだけ、ルーターは catch を書かない。どのステータスを返すかは、個々のエンドポイントの関心ではないからです。

```python
# エラー → HTTP ステータスの対応表を一箇所で登録する
def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NotFoundError, _to_404)
    app.add_exception_handler(DuplicateError, _to_409)

# ルーターの中でハンドリングをする必要がなくなる
@router.get("/{id}")
async def get_article(id: int, service: ArticleService = Depends(...)):
    return await service.get_article(id)
```

それがどの層の仕事なのかを先に見るようになったことが、「分かりやすいコードとは何か」を考えるきっかけになりました。

## 1.5 第1幕の終わりに

自分は最初、コードを理解するために、正直に上から順番に処理を追っていきました。なんとか全体像を掴もうとする。けれど、何も分かっていない状態でそれを続けるのは、とても疲れる作業でした。

そこから少しずつ、「誰が何のために使うのか」「何を達成すべきなのか」から逆算して見るようになりました。その視点に変わったことで、ただ処理を追っているだけでは気づけなかったことが見えるようになっていきました。

このレビューで一番強く感じたのは、API 設計の難しさでした。DB には正規化理論のような手がかりがあって方向を決められましたが、API は「これがどう使われるのか」を想像しながら設計するしかなく、明確な正解がありません。

当初は、DB さえ正しく理解して設計できれば、その先はデータを受け渡していくだけで、スムーズに進むと思っていました。でも実際には、一つの層だけを切り離して決められる場所はどこにもなく、全体の流れを意識しながら設計する必要がありました。

この経験は、自分にとってとても大きかったと思います。行き詰まったときに、同じ見方のまま無理に読み進めるのではなく、視点を変えてみること。処理の流れだけを追うのではなく、何のためのコードなのか、誰のための API なのかから考え直してみること。その大切さを、このレビューを通じて学びました。

一方で、同じ時期には、より良い設計を求めて書籍を読み、セキュリティや secure-by-design、値オブジェクトといった新しい概念を、自分のコードに取り入れようとしていたのです。次の幕では、少し時間を戻して、その試行錯誤を振り返ります。

> 次: [第2幕 — 値オブジェクトを入れる](02-value-objects.md)
