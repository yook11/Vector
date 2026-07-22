[← 目次](README.md) ・ 次: [第2幕](02-value-objects.md)

# 第1幕 — 自分の意図で設計し直す

「自分でコードを理解できるようになりたい」。そう思って、最初に取り組んだのがこのレビューでした。

当時は、ユーザーからのリクエストを受け取り、処理を経てデータが返されるまでの流れすら、まだ把握できていませんでした。
コードベースを開いても、どこで何が行われているのかまったくわからない。そんな状態からのスタートでした。

当時、自分が一番理解できる可能性がある部分はどこなんだろう？と考えて、『達人に学ぶ DB設計 徹底指南書』と『SQLアンチパターン』の 2 冊を読んでいた経験から、データベース、とりわけ ORM の部分から見ていこうと決めました。

## 1.1 まず、モデルと DB から

まずは、現状を棚卸しすることにしました。Claude Code に現在のテーブル設計を書き出してもらい、その内容を一つひとつ確認するところから始めました。

そこで気づいたのは、設計の良し悪しを考える以前に、ORMとして定義された Python 側のモデルと、実際のデータベースが別のレイヤーに存在することさえ理解できていなかった、ということです。

models を書き換えれば、そのままデータベースも変わる。そう思い込んでいました。  
しかし実際には、models は「データベースはこうあるべき」という定義であり、その変更を実際のデータベースへ反映するには、Alembic の migration が必要です。このレビューを始めて、初めてその仕組みを理解しました。

しかし、このレビューを通じて本当に向き合うことになったのは、知識不足そのものではありませんでした。
棚卸しした内容を一つひとつ確認するうちに、ある事実に気づきました。このテーブル構成は、自分で設計したものではなかったのです。

例えば、ニュースソースのテーブルでは、ニュースの取得元を管理するだけのはずが、次のような列が並んでいました。

| column | meaning |
|---|---|
| `id` | ソースID |
| `name` | ソース名 |
| `source_type` | RSS / API |
| `site_url` | サイトURL |
| `is_active` | 有効フラグ |
| `fetch_interval_minutes` | 取得間隔（分） ← 誰も設定しない |
| `next_fetch_at` | 次回取得予定時刻 ← 使われない |
| `last_fetched_at` | 最終取得時刻 ← 使われない |
| `consecutive_errors` | 連続エラー回数 ← 集計されない |
| `last_error_message` | 最終エラー内容 ← 読まれない |
| `feed_url` | RSS用URL（API時はNULL） |
| `etag` | HTTPキャッシュ用 ← 実装されていない |
| `last_modified_header` | HTTPキャッシュ用 ← 実装されていない |
| `api_endpoint` | API用（RSS時はNULL） |
| `created_at` | 作成時刻 |
| `updated_at` | 更新時刻 |

その多くは、実装されてから一度も使われていませんでした。

ここで思い知らされました。コーディングエージェントを使えば、提案を承認していくだけでも、それらしいアプリケーションは出来上がっていきます。
それまでの開発では、AI が主導し、自分は提示された設計を受け入れ、並べられた選択肢の中から選んでいただけでした。「このアプリケーションでは、データをどのように扱うべきなのか」、自分自身で一度も考えていなかったのです。

そこで、テーブルの役割そのものを見直すことにしました。取得間隔や次回取得時刻、エラー履歴、HTTPキャッシュなど、使用されていなかった列は削除しました。また、RSS用の feed_url とAPI用の api_endpoint は、どちらも「ニュースの取得先」を表すため、endpoint_url に統合しました。

| column | meaning |
|---|---|
| `id` | ソースID |
| `name` | ソース名 |
| `source_type` | 取得方法 |
| `site_url` | サイトURL |
| `endpoint_url` | 実際の取得先URL |
| `is_active` | 有効フラグ |
| `created_at` | 作成時刻 |
| `updated_at` | 更新時刻 |

この気づきが、出発点でした。ここから初めて、自分の意図で一つずつ決め直していくことになります。


## 1.2 「結局ユーザーは何を見たいのか」

API が返すデータをどう直すべきか、最初は正しい設計が何かも、どのようにコードを見ていけばいいのかさえ分かりませんでした。

何から手をつければよいのか分からなかったため、DB のときと同じように、まずは技術書に頼ることにしました。『API デザインパターン』を読み始め、並行して設計のベストプラクティスを AI に調べてもらいました。
「命名はシンプルにする」「前置詞は危険信号」「用途が名前から読み取れるようにする」——設計を考えるための知識は、少しずつ増えていきました。

ただ、得た知識をすぐに活かせたのは、命名の方向性を揃えることくらいでした。肝心の「何を、どのような基準で設計し直すべきなのか」は、全く見えていませんでした。
知識を増やしても、コードを読めば「ここで定義されている型は、どこで何のために使われているのか」と立ち止まってしまいます。エンドポイントを眺めながら壁打ちを繰り返しても、自分が納得できる答えにはたどり着けませんでした。

そんな中、ふと「実際にそのデータが使われている箇所から、逆算して見ればよいのではないか」と考えました。
本当に必要な情報は何か。結局、ユーザーは何を見たいのか。コードを起点にするのではなく、実際にデータが使われる側から見直してみることにしました。
すると、それまで気づけなかった問題が、少しずつ見えるようになってきました。

このアプリの中核にあるのは「AI 分析済みニュース」です。ところが、API レスポンスは取得した分析する前の記事を中心に設計されていました。
型の名前は NewsResponse。最上位には原文タイトルや元記事の URL が並び、AI による分析結果は、省略可能な一フィールドとしてその下にぶら下がっていました。
型の構造そのものが、このアプリケーションで何をユーザーに届けたいのかを、まったく表していなかったのです。

```python
class NewsResponse(_CamelBase):
   # 型の主語は「ニュース」。分析する前の記事がレスポンスの中心に置かれていた。
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
    # ユーザーが実際に見ていた AI 分析は、この型の中に閉じ込められていた。
    translated_title: str
    summary: str
    impact_level: ImpactLevel
    reasoning: str
    ai_model: str
    analyzed_at: datetime
```

コードと実際の画面を照らし合わせたことで、問題は単なる命名ではなく、データの主従関係そのものにあると分かりました。

加えて、一覧画面と詳細画面が同じレスポンス型を共有しており、一覧カードでは使わない情報まで毎回返していました。レスポンスを設計するときに、そのデータがどの画面で、どのように使われるのかまで考えられていなかったのです。

そこで、ユーザーが実際に読む AI 分析を中心に、API レスポンスを設計し直しました。また、一覧と詳細では必要な情報が異なるため、それぞれの用途に合わせて型を分け、使わないデータを含めない形にしました。

```python
class NewsBrief(_CamelBase):
    # 一覧カードでユーザーが読む情報を、レスポンスの主役にした。
    id: int
    translated_title: str
    summary: str
    impact_level: ImpactLevel
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

    # 分析する前の記事は、必要なときに参照する補助情報として分離した。。
    original: OriginalArticleEmbed


class OriginalArticleEmbed(_CamelBase):
    title: str
    url: SafeUrl
    content: str | None = None
```

「そのデータが、実際にどこでどのように使われているのか」から逆算して考えるようになったことで、何を直すべきかが少しずつ見えるようになりました。ここで得た視点は、その後のレビューでも判断の軸になりました。


## 1.3 エンドポイントレビュー

次にエンドポイント層を確認すると、これまで見てきたどの層よりも強い違和感を覚えました。リクエストの受け付けから、DB クエリの組み立て、データの変換、レスポンスの生成まで、すべての処理が一つのハンドラに詰め込まれていたのです。

当時は、プロジェクト作成時のデフォルト構成をそのまま使っており、コードの流れを追いにくい状態でした。
まずはこの読みにくさを解消しようと考えましたが、どのように整理すべきか分からず、FastAPI におけるレイヤー分離とファイル構成のベストプラクティスを調べることにしました。

調べる中で見つけた記事から、コードを Router、Service、Repository に分け、それぞれに役割を持たせる構成を知りました。
リクエストの受け付けは Router、ビジネスロジックは Service、DB へのアクセスは Repository。フォルダで役割ごとに分けて整理するという考え方を知ったのは、このときでした。

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

URL だけを見ると、どれも `/news` という一つのまとまりに属する API に見えます。しかし、実際には、ユーザーが記事を読むための API と、管理者がニュースの取得や AI 処理を動かすための API が、同じルートの下に並んでいました。
その結果、`/news` が「画面に表示する分析済みニュース」を表すのか、「ニュース取得や AI 処理を操作する入口」を表すのかが曖昧になり、この API が何のために存在するのか分かりにくくなっていました。

そこで、API の境界を「ニュース」というデータ名ではなく、「誰が、何のために使うのか」という目的に沿って切り直しました。
ユーザーが扱うものは `/articles`、管理者が操作する処理工程は `/pipeline`。先ほど切り出した Service と Repository もこの境界に合わせて配置し直し、記事の参照とパイプラインの操作を、それぞれ別のファイルへ分離しました。


### 使用されていないエンドポイント

次に、各エンドポイントがどこから呼ばれているのかを調べると、実際には使われていないものがいくつも見つかりました。
なかでも `keywords` の CRUD は、明確な用途があって設計した機能ではなく、「いつか使うかもしれない」という理由だけで残されていました。ここでも、自分で必要性を考えずにAIの提案を受け入れていたことを痛感しました。
使われていない機能を残せば、保守するための負担になります。そのため、keywords の CRUD は削除しました。

記事のベクトルを生成するエンドポイントも、どこからも呼ばれていない未使用のものでした。しかし当時は、「生成に失敗した記事を、手動で処理し直すときに使うかもしれない」と考え、削除せずに残しました。

いま振り返ると、この判断にも、「必要になったときに改めて設計する」のではなく、「いつか使うかもしれないから残しておく」という迷いが表れています。未使用のコードを削る必要性には気づき始めていたものの、その考えを一貫して適用できるほど、まだ自分の判断軸は固まっていなかったのだと思います。


## 1.4 コードの置き場所

Router、Service、Repository に役割を分けたことで、リクエストを受け取ってから、処理がどの層を通ってデータへたどり着くのかを追えるようになりました。
そこで今度は、エンドポイントを一つずつ、「何のために存在し、何をすべきなのか」という目的から見直していきました。

たとえば当時の記事一覧 API は、次のような URL で呼ばれる想定でした。

`GET /api/v1/articles?q=quantum&category=ai&impactLevel=medium&sortBy=publishedAt&sortOrder=desc&page=1&perPage=12`

しかし、q だけは router の関数引数として直接受け取る一方、category や impactLevel、sortBy、perPage は、FastAPI の Depends() を通して ArticleListParams というオブジェクトにまとめて受け取っていました。

どれも同じ URL から渡されるクエリパラメーターなのに、コード上では受け取り方が二つに分かれていたのです。

受け取り方を一か所にまとめようと ArticleListParams の定義を確認すると、そのクラスは Repository に置かれていました。
URLから受け取るクエリパラメーターの名前や形式は、本来HTTPの入口で扱うものです。しかし、その知識がDBアクセスを担当する層にまで入り込んでいたのです。

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

コードを分かりやすくするには、処理をまとめるだけでなく、それぞれの役割に合った場所へ置くことも重要なのだと学びました。

そこで、HTTP から受け取る入力と、Service や Repository が使う検索条件を別の型に分け、その変換を入口となる Router で行うようにしました。

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


先ほどの ArticleListParams には、もう一つ問題がありました。不正なカテゴリを受け取ると、Repository の中で HTTPException を発生させ、422 エラーを返していたのです。DBアクセスを担当する層が、HTTPのステータスコードまで知っている状態でした。

```python
# repositories/articles.py

class ArticleListParams:
    def __init__(
        self,
        category: Annotated[str | None, Query()] = None,
        ...
    ) -> None:
        try:
            self.category_slug = CategorySlug(category) if category else None
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid category slug: {category!r}",
            )
```

一方、記事が見つからなかった場合には、各 Router が例外を受け取り、404エラーへ変換していました。こうした変換が共通化されておらず、同じような try/except が複数の Router に繰り返し書かれていたのです。

```python
@router.get("/{id}")
async def get_article(id: int, service: ArticleService = Depends(...)):
    # 各ルーターに同じ変換が並んでいた
    try:
        return await service.get_article(id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="...")
```

そこで、どの例外をどの HTTP ステータスへ変換するのかを、アプリケーション全体の例外ハンドラーにまとめて登録しました。
例外をどの HTTP レスポンスに変換するかは、個々のエンドポイントではなく、アプリケーション全体で統一して扱うべきものだと考えたからです。

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

処理の内容だけでなく、「それはどの層が担うべき仕事なのか」を先に考えるようになったことが、分かりやすいコードとは何かを見直すきっかけになりました。


## 1.5 第1幕の終わりに

このレビューを通じて最も強く感じたのは、API 設計の難しさでした。DB には正規化理論のような判断の手がかりがあり、それをもとに方向性を決められます。一方、API は「誰が、何のために、どのように使うのか」を考えながら形を決める必要があり、一つの明確な正解があるわけではありません。

当初は、DB さえ正しく理解して設計できれば、その先はデータを受け渡していくだけで、スムーズに進むと思っていました。
しかし実際には、どの層も単独で設計できるものではありません。ユーザーからのリクエストがどのように処理され、どのような形で返されるのか。その全体の流れを意識しながら、それぞれの層を設計する必要がありました。

このレビューは、自分にとってコードの見方を変える大きな経験になりました。行き詰まったときは、同じ見方のまま無理に読み進めるのではなく、視点を変えてみる。処理がどう動くのかだけでなく、「何のためのコードなのか」「誰のための API なのか」に立ち返る。その大切さを、このレビューを通じて学びました。

このレビューを進めていた同じ時期、よりよい設計を求めて書籍を読み、セキュリティや Secure by Design、値オブジェクトといった新しい考え方を、コードに取り入れようとしていました。
次の幕では少し時間を巻き戻し、それらの考え方をコードに落とし込もうとした試行錯誤を振り返ります。

> 次: [第2幕 — 新しい概念に出会い、取り入れる](02-value-objects.md)
