[← 目次](README.md) ・ 前: [第2幕](02-value-objects.md)

# 第3幕 — 小さな違和感から、「ドメイン」に手を伸ばす

第2幕で試した値オブジェクトには、確かな手応えがありました。その値が存在すること自体が「検証済みである」という保証になる。型によって不正な状態を排除できることに、より良い設計へ近づいている実感がありました。

書籍を読み進めるうちに、私は少しずつ「ドメイン」という考え方に触れていきました。

ドメインとは何かを十分に理解できていたわけではありません。それでも、コードを単なる処理の手順ではなく、アプリケーションの中にある意味や責任を表すものとして捉えようとしていました。


## 3.1 小さな違和感から

当時、ニュースソースの有効・無効を切り替える処理では、Serviceが`source.is_active = not source.is_active`のように値を直接書き換え、変更したオブジェクトをRepositoryの`save()`に渡していました。

```python
# Service
async def toggle_source(self, source: NewsSource) -> NewsSourceDetail:
    source.is_active = not source.is_active
    source = await self.repo.save(source)
    return NewsSourceDetail.model_validate(source)

# Repository
async def save(self, source: NewsSource) -> NewsSource:
    self.session.add(source)
    await self.session.commit()
    await self.session.refresh(source)
    return source
```

コードを読んでも、操作の意図がすぐには伝わってきません。

やりたいことは、「ニュースソースを有効化する」あるいは「無効化する」ことです。しかしコード上では、「`is_active`の値を反転して保存する」という実装上の手順として表現されていました。

呼び出し側から見たときに、何をしようとしているのかが明確でなければならない。そう考え、メソッド名だけで操作の意図が伝わるように、Repositoryに`activate()`と`deactivate()`を定義しました。

```python
async def activate(self, source: NewsSource) -> NewsSource:
    source.is_active = True
    self.session.add(source)
    await self.session.commit()
    await self.session.refresh(source)
    return source

async def deactivate(self, source: NewsSource) -> NewsSource:
    source.is_active = False
    self.session.add(source)
    await self.session.commit()
    await self.session.refresh(source)
    return source
```

呼び出し側も、次のように書けるようになりました。

```python
source = await self.repo.activate(source)
```

「値を反転して保存する」ではなく、「ニュースソースを有効化する」。操作の意図を、そのままコードから読み取れるようになりました。


## 3.2 トランザクションの境界

当初の私は、データベースへの保存はRepositoryの仕事であり、`commit`までRepositoryが行うものだと考えていました。しかし、AIとの対話を重ねる中で、その考えを見直すことになります。

Repositoryが変更のたびに`commit`してしまうと、複数の変更を一つの操作として扱うことが難しくなります。
処理の途中で失敗すれば、一方の変更だけが確定し、もう一方は反映されないという、中途半端な状態が残るかもしれません。

このとき初めて、私は「一つのユースケースに含まれるDB更新を、一つのトランザクションとして扱う」という考え方を知りました。

そこで当時は、Repositoryの各メソッドから`commit`を外し、ユースケースを組み立てるService側で、処理の最後にまとめて`commit`するようにしました。Repositoryが変更を個別に確定するのではなく、ユースケース全体を一つの単位として確定するためです。


## 3.3 振る舞いをどこに置くか

`commit`を外したあと、Repositoryの`activate()`と`deactivate()`には、状態を変更する処理だけが残りました。

```python
async def activate(self, source: NewsSource) -> None:
    source.is_active = True

async def deactivate(self, source: NewsSource) -> None:
    source.is_active = False
```

このコードを見たとき、私は新たに知った「振る舞い」という考え方を、ここに取り入れられるのではないかと思いました。

改めてRepositoryに残った処理を見ると、行っているのはデータの保存ではありません。すでに取得されている`NewsSource`の状態を変えているだけです。

であれば、これはRepositoryの処理ではなく、`NewsSource`自身の振る舞いとして表せるのではないか。そう考え、`activate()`と`deactivate()`を`NewsSource`モデルに置いてみることにしました。

```python
class NewsSource:
    def activate(self) -> None:
        self.is_active = True

    def deactivate(self) -> None:
        self.is_active = False
```

ServiceはRepositoryから`NewsSource`を取得し、目的に応じたメソッドを呼び出します。そして、ユースケースの最後に変更を確定します。

```python
class NewsSourceService:
    def __init__(
        self,
        repository: NewsSourceRepository,
        session: AsyncSession,
    ) -> None:
        self.repository = repository
        self.session = session

    async def activate(self, source_id: int) -> None:
        source = await self.repository.get(source_id)

        source.activate()

        await self.session.commit()

    async def deactivate(self, source_id: int) -> None:
        source = await self.repository.get(source_id)

        source.deactivate()

        await self.session.commit()
```

今振り返ると、管理画面で使う単純な状態変更に、設計投資をする必要はないと思います。
当時は、「振る舞い」を実装してみたいという気持ちが先行していました。

それでも、この処理を書いたことで、別の疑問が生まれました。


## 3.4 メモリ上の状態とDBの状態

`source.activate()`の中で行っているのは、`self.is_active = True`という代入だけです。

メモリ上の変数を書き換えるだけで、なぜそれがDBの更新につながるのか。DBの値は、いつ、どのような仕組みで書き換えられるのか。当時の私は、その流れをイメージできていませんでした。

調べていくうちに、属性を変更した時点で、DBの行が直接書き換えられるわけではないことが分かりました。

まず、DBから取得された`NewsSource`は、アプリケーションのメモリ上にオブジェクトとして展開され、SQLAlchemyのSessionに関連付けられます。そのオブジェクトの属性を変更すると、Sessionが変更を記録します。

その後、ユースケースの最後に`commit`すると、SQLAlchemyがメモリ上の変更をDBへ反映し、トランザクションを確定します。反対に、処理が失敗して`rollback`されれば、確定していない変更は取り消されます。

この仕組みを知って、`source.activate()`が変えているのはDBではなく、メモリ上にある`NewsSource`の状態なのだと分かりました。まずオブジェクトの状態が変わり、その変更が後からDBへ反映されます。

それまでの私は、コードで書き換えた値が、いつ、どのようにDBへ反映されるのかを意識していませんでした。
振る舞いの置き場所を考えたことが、メモリ上の状態変更とDBへの反映を、初めて分けて捉えるきっかけになりました。


## 3.5 コードの置き場所を、作業の単位で考える

この頃、コードを直すたびに引っかかっていたことがありました。

たとえば、「記事を分析する」という処理を理解するには、まずタスクキューから呼び出される入口を探し、そこからServiceへ処理をたどる必要がありました。さらに、分析対象の記事を取得し、必要なデータとプロンプトを組み立て、Geminiを呼び出す。返ってきた応答を確認し、最後に結果をDBへ保存するところまで、複数の場所を追っていきます。

一つの作業としては「記事を分析する」だけです。けれど当時のコードでは、その流れが複数の場所に分かれていました。

```text
app/
├── tasks/
│   └── analysis_tasks.py      # タスクキューから呼び出される入口
├── services/
│   ├── ai_analyzer.py         # 記事分析の一連の流れを組み立てる
│   └── gemini_analyzer.py     # Geminiを呼び出し、応答を処理する
└── repositories/              # 分析対象の記事を取得し、分析結果を保存する
```

どのファイルも、置き場所として間違っているわけではありません。けれど、理解するには時間がかかり、素直にしんどいと感じるようになっていました。

そのとき頭にあったのが、「ドメインのまとまりで分ける」という考え方でした。取得・分析・保存といった実装上の役割ではなく、「アプリの中で何を実現する処理なのか」という単位で捉え直す。

最初に手をつけたのは、AI 分析まわりでした。AI モデルの差し替えやプロンプトの見直しなど、今後変更することが明確だったからです。作業の単位でまとめれば、変更するときにも読みやすくなるのではないかと考えました。

最初は要約、翻訳、分類、キーワード抽出まで、細かくファイルを分けようとしました。しかし、これらは一つのプロンプトでまとめて処理していたため、分けると一回の分析を追いにくくなります。それでは本末転倒だと考え、独立した処理であるAI分析とベクトル生成の二つに留めました。

「記事を分析する」というまとまりが見えるように、`app/analysis/`を作成しました。

```
app/
└── analysis/
    ├── __init__.py          # analysisパッケージが公開する処理を定義
    ├── service.py           # 記事の分析と保存までの流れを組み立てる
    ├── analyzer/
    │   ├── __init__.py
    │   ├── base.py          # AI分析の共通処理と戻り値を定義
    │   ├── factory.py       # 使用するAnalyzerを生成
    │   └── gemini.py        # プロンプトの構築、Geminiの呼び出し、応答の変換
    ├── embedder/
    │   ├── __init__.py
    │   ├── base.py          # 埋め込み生成の共通処理を定義
    │   ├── factory.py       # 使用するEmbedderを生成
    │   └── gemini.py        # Geminiを使った埋め込み生成
    ├── dedup.py             # 埋め込みを使った重複記事の検出
    └── errors.py            # 分析処理で共通して扱うエラーを定義
```

同じ流れで、ニュースを集める処理は `collection/`、分析結果を使って検索する処理は `search/` へ分けていきます。

この構成は、その後のリファクタリングで変わっています。
それでも、この経験から、コードを概念に沿って整理することで、処理の流れを追いやすくなり、理解や変更にかかる負担も減らせることを学びました。


## 3.6 構造と型でドメインを表す

このアプリケーションでは、本文を取得できた記事だけを分析対象としていました。本文のない記事は、後続の分析処理には進めないという決まりです。
当時、収集した記事はすべてNewsArticleという一つのテーブルで扱っていました。

| カラム | 役割 |
|---|---|
| `id` | 記事ID |
| `original_title` | 元記事のタイトル |
| `original_url` | 元記事のURL |
| `original_description` | 配信元から取得した概要・説明文 |
| `news_source_id` | 取得元のニュースソース |
| `published_at` | 公開日時 |
| `original_content` | 記事本文。取得できるまでは`NULL` |
| `skip_content_fetch` | 本文取得を断念し、分析へ進めないことを表すフラグ |

このテーブルには、記事そのものの情報と、処理の進行状況を管理するためのフラグが混在していました。

そのため、本文の取得やリトライ、分析対象の選択といった処理では、記事がどの状態にあるのかを複数のカラムから判断する必要がありました。
たとえば、記事を分析に進められるかどうかを、カラムに値が入っているかで判定していました。

```python
for article in source_result.new_articles:
    # 記事の本文が取得できていれば分析の工程に進める
    if article.original_content is not None:
        await extract_content.kiq(article.id)
    # 不足している場合別の工程へ
    else:
        await fetch_content.kiq(article.id)
```

`NewsArticle`には、ニュースソースから見つけた段階の記事と、本文を取得して分析に進める状態になった記事という、二つの異なる概念が混ざっているのではないかと考えました。

そこで、フラグによる状態管理をやめ、この二つを別のテーブルに分けることにしました。記事を見つけた時点では、URLやタイトル、取得元を記録します。そして、本文の取得に成功したときだけ、分析対象の記事として別の行を作ります。

|  | `discovered_articles` | `articles` |
|---|---|---|
| 表すもの | ニュースソースから見つけた記事 | 分析に進める状態の記事 |
| 作成するタイミング | 記事を見つけたとき | 本文の取得に成功したとき |
| 主なデータ | 取得元、URL、タイトル、発見日時 | 元記事との関連、タイトル、本文、公開日時 |
| 行が存在する意味 | 記事を発見済み | 分析に必要なデータを取得済み |

`articles`では本文を必須にしました。本文を取得できたときだけ行が作られるため、その行が存在すること自体が「分析に進める」という保証になります。これにより、処理のたびに本文の有無を確認する必要がなくなりました。

テーブル構造を見直す中で、本文取得処理の戻り値にも気になる点が見つかりました。

当時は、取得結果を`status`と`article_id`で表していました。`status`には処理の結果が入り、`article_id`には次の分析処理へ渡す記事のIDが入ります。

```python
@dataclass(frozen=True)
class ContentFetchResult:
    status: Literal["fetched", "already_exists", "skipped"]
    article_id: int | None = None
```

しかし、この型では二つの値の関係を保証できません。「取得に成功したのにIDがない」「スキップしたのにIDがある」といった、本来はあり得ない組み合わせも作れてしまいます。

ここで、ただ型を作るだけでは意味がなく、何を保証したいのかを先に考える必要があると気づきました。今回守りたいのは、「取得できた結果には`article_id`があり、スキップした結果にはない」という条件です。その条件が崩れないように、型の構造で表すことにしました。

```python
@dataclass(frozen=True)
class Fetched:
    article_id: int

@dataclass(frozen=True)
class AlreadyExists:
    article_id: int

@dataclass(frozen=True)
class Skipped:
    pass

ContentFetchResult = Fetched | AlreadyExists | Skipped
```

何を保証したいのかを考え、その条件を型や構造で表す。条件を後から確認するのではなく、満たした状態だけを扱える形にする。こうした発想が、この頃から少しずつ生まれ始めました。

## 3.7 第3幕の終わりに

値オブジェクトを導入したときと同じように、この時期も、新しく知った設計手法を試すことが先に立ち、優先度の高くない部分に時間をかけることがありました。

ここで触れた具体的な設計も、現在のコードにはほとんど残っていません。`NewsArticle`はなくなり、記事テーブルの分け方や、`Fetched | AlreadyExists | Skipped`という戻り値の型も、その後のリファクタリングで作り直されました。

当時は、何を型で表すべきなのか、本当に保証したい条件は何なのかを、十分に考えきれていませんでした。設計の形を決める前に、対象となる処理の流れを理解しようとする姿勢も足りなかったと思います。そのため、当時の設計には、まだ多くの粗さが残っていました。

それでも、何を保証したいのかを先に考え、その条件を型やテーブルの構造で表すという発想は、この時期に少しずつ生まれました。条件を後から分岐で確認するのではなく、間違った状態をできるだけ作れない形にする。その考え方は、この後の設計を考える土台になっていきます。

ここまでは、設計を良くしたいという思いに引っ張られていた部分がありました。
次の第4幕では、設計の形そのものではなく、「このアプリで価値を生むものは何か」から考え直していきます。

次: [第4幕 — このアプリケーションの価値とは？](04-investing-in-value.md)
