[← 目次](README.md) ・ 前: [第5幕](05-audit-makes-separation-real.md)

# 第6幕 — アプリケーションの概念と向き合う

第6幕では一つの概念として扱っていたものを紐解いていきます。
中心になるのは、それまで AI の提案を受けながら作ってきた型や抽象を、「この型は何を保証するのか？」を問いながら、自分の理解で設計し直していきます。

なお、この時期は語彙の統一や改名、設計の見直しが並列して走っていたため、現在の設計とは違うものが多々あります。

## 6.1 Ready を、「次へ進む合図」から「処理開始時点の保証」へ作り直す

第3幕で、私は型がただのデータの形ではなく、満たさなければならない条件を表せるものだと知りました。

その考えを、4月末に非同期パイプラインへ広げようとしていました。パイプラインでは、ある工程から次のタスクへ記事を渡すときに、満たしていなければならない条件があります。
そうした条件を `ReadyFor {ステージ名}` という型として定義すれば、次の工程に渡せるものだけを明確にでき、より堅牢な設計になると考えました。

タスクに投入する前に「次の処理に進めること」を確認できれば、それで十分だと考えていました。上流の Task が DB を見て、未処理であることや必要な値が揃っていることを確認し、ReadyForX を作ってキューに積む。そうすれば、下流の Task はその型を受け取った時点で、前提条件を満たしているものとして処理へ進めるはずだと考えていました。

```python
class ReadyForClassification(BaseModel):
    extraction_id: int
    translated_title: str
    summary: str

class ReadyForExtraction(BaseModel):
    article_id: int
    original_title: str
    original_content: str
```

しかし、ここで見落としていることがありました。前の工程が終わると、その結果は DB に永続化されます。一方で、次の工程には、型に詰めた値を渡していました。

私は条件を型で表しているつもりでしたが、実際には、DB に保存された事実と Ready にコピーした値の整合性をどう保つかまでは考えられていませんでした。

そうなると、処理に使う値の正本をどちらに置くのかを決める必要があります。DB に永続化した値を正本とするなら、次の工程へ値そのものを渡すのではなく、ID だけを渡して、次の工程が DB から読み直せばよい。そう考えて、一度は ID だけを次の処理へ渡す発想に向かいました。

```python
class ReadyForEmbedding(BaseModel):
    model_config = ConfigDict(frozen=True)

    analysis_id: int = Field(gt=0)
```
次の処理では、この ID を使って DB からデータを読み直し、処理を開始するようにしました。DB を正本にするという意味では、こちらのほうが筋が通っているように見えました。

けれど、この型を見たときに、別の違和感が残りました。

条件を保証するための型のはずなのに、ID を運んでいるだけの薄いラッパーになっていたのです。

そこで気づきました。前の工程が「次の処理に進める」と保証するのではなく、その処理を始める工程自身が、開始直前に「いま処理できる」と保証するべきだったのではないか、と。

```python
class ReadyForEmbedding(BaseModel):
    model_config = ConfigDict(frozen=True)

    analyzed_article_id: int = Field(gt=0)
    text_for_embedding: str = Field(min_length=1)
    analyzable_article_id: int = Field(gt=0)
```

本来 Ready が保証すべきだったのは、実際にその処理を始める時点で、処理ごとの前提条件を満たしていることでした。

前の工程から渡すのは ID だけにする。現在はトリガーという型で表しています。
次の工程は、その ID を受け取ってから、 Ready を構築する。
こうして Ready は、「次へ進めるはず」という合図ではなく、処理する側が開始条件を満たしていると確認した証明になりました。

大事なのは、その型が何を、いつ、誰の責任で保証するのかを決めることです。守りたい条件が曖昧なまま型を作ると、名前だけが強くて、実際には何も守れていない型になってしまう。だからこそ、どこで何を保証する必要があるのかを考えてから、型を設計する必要があると学びました。


## 6.2 型を見直していく

Ready を見直したことで、それまで自分が導入してきた他の型は、いったい何を保証しているのか、が気になり始めました。一つずつ見ていくと、工程と工程の境界を跨ぐところの多くで、何も保証していない型が、いくつもあることに気づきました。

当時のAI分析の結果の型は、タイトルと要約と投資家向けの示唆が揃っていて、それぞれ空でないことを定義していました。

```python
# in-scope と判定された評価結果。何が揃えば「評価結果」と呼べるかを型で保証する。
@dataclass(frozen=True)
class InScopeAssessment:
    id: int
    extraction_id: int
    translated_title: str   # 翻訳タイトル
    summary: str            # 要約
    investor_take: str      # 投資家向けの示唆
    topic: TopicName
    category_id: int
    ai_model: str
    analyzed_at: datetime

    def __post_init__(self) -> None:
        if not self.translated_title:
            raise ValueError("translated_title must be non-empty")
        if not self.summary:
            raise ValueError("summary must be non-empty")
        if self.id <= 0:
            raise ValueError("id must be positive")
        # investor_take / ai_model / extraction_id ... も同様に検証
```

ところが、AI処理を実行している部分はこの型をそのまま返さず、もう一段ラップした型で返していました。

```python
# 評価結果を、もう一段ラップしただけの型。
@dataclass(frozen=True)
class InScopeOutcome:
    assessment: InScopeAssessment

AssessmentOutcome = InScopeOutcome | OutOfScopeOutcome
```

このラップされた型を受け取った後に特別な分岐はなく、種類を見て中身を取り出しているだけでした。

```python
if isinstance(result, InScopeOutcome):
    ready = ReadyForEmbedding.from_assessment(result.assessment, ...)
```
そこで、この結果型を畳み、評価結果をそのまま返すようにしました。

```python
# 3つのラッパーを消し、評価結果そのものを返す。
async def execute(...) -> InScopeAssessment | OutOfScopeAssessment:
    ...

if isinstance(result, InScopeAssessment):
    ready = ReadyForEmbedding.from_assessment(result, ...)
```

この型を消したとき、処理は一行も変わりませんでした。この型は何も保証をしていなかったのです。
同じように、何も保証していないのにただ包んでいるだけの型が、ほかの工程の境界にもいくつも残っていました。

それまでは境界を跨ぐ時には、型を使用する程度のことしか考えることができていなかったんだと思います。
型で何を保証するということの意味を考えることができていなかったことを痛感しました。


## 6.3 ニュースを取得する工程に目を向ける

AI 分析の Ready を見直したことで、分析に進める品質を持った記事とは何かも、改めて見直すことになりました。

それまでも、第3幕で「見つけた記事」と「分析に進める記事」は分けていました。まず URL とタイトルを保存し、その URL を後から開いて本文を取得する。本文が取れて、`articles` に行を作れたものだけが、分析対象になる。つまり、分析に進める記事であることは、主に DB の構造と行の存在によって表していました。

けれど、工程の出口として「どんな条件を満たせば分析に進めるのか」は、まだはっきりした型になっていませんでした。

そこで、最初に取得工程の出口を定義しました。

```python
class FetchedArticle(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=50, max_length=1_048_576)
    published_at: PublishedAt
    source_id: int = Field(gt=0)
    source_url: SafeUrl


class Ready(BaseModel):
    article: FetchedArticle

class Failed(BaseModel):
    reason: FailureReason

FetchOutcome = Ready | Failed
```

当時の取得工程は、まず記事の URL とタイトルを保存し、その URL を後から開いて本文を取得する設計になっていました。
最初の工程では、記事のURL「記事を見つけた」ことを記録をする。後続のスクレイピング工程で情報を取得しにいく。

この時は、この設計で問題ない、綺麗に責任がわかれていると考えていました。


### 工程自体の問題点

この設計には見落としがありました。
ソースによっては、配信された時点で、すでに分析に進める条件を満たしている記事があったのです。

手順を統一していたせいで、後から改めて同じページを開きに行ったときに、Bot 対策に阻まれ、スクレイピングそのものが失敗することがありました。配信の時点で本文が揃っていたのに、それを使わずに捨て、記事ごと失っていたのです。

そこで、取得の出口を二つに整理しました。配信時点で分析に進める品質が揃っていれば、そのまま分析へ進める。足りなければ、取れた情報を保存し、後で HTML から本文を補う工程へ回す。

ただ、この整理だけでは足りませんでした。外部から取れた値は、ソースごとに配信形式が違います。
それを「共通の形に揃える」ことと、「分析に進める保証があるか判断する」ことを、一つの型で同時にやることになり、分析に進めるかどうかの判断が、配信形式の違いにまで引きずられていたのです。

そこで、「形式を読む」「共通の形に揃える」「分析に進める保証を判断する」を、別々の役割に分けました。

外部の形式を読むのは `Reader` の役割にしました。`Reader` は、RSS、sitemap、HTML listing、API といったソースごとの形式の違いを読み取り、その結果を、形式ごとの軽い `Entry` として返します。`Entry `は、まだ意味づけをしない、その形式のまま取れた値を写した箱です。ここでは記事かどうかも、分析に進めるかも判断しません。形式の違いだけを、ここで吸収します。

```python
@dataclass(frozen=True, slots=True)
class RssEntry:
    title: str | None
    link: str | None
    summary: str | None
    published: str | None


@dataclass(frozen=True, slots=True)
class HtmlListingEntry:
    title_text: str | None
    href: str | None
    excerpt_text: str | None
    published_text: str | None
```

その Entry を、形式の差を落とした共通の中間型 FetchedArticle に揃えます。

```python
@dataclass(frozen=True, slots=True)
class FetchedArticle:
    """外部ソースから取れた値を、まだ判断せずに共通の形へ揃えたもの。"""

    title: str
    url: str
    body: str | None
    published_at: datetime | None
```

そして、 convert_fetched_article がアプリケーション内部の概念へ翻訳します。
そのまま分析に進めるなら `AnalyzableArticle`。分析に進めなかった場合も、後続工程で補えるように、 `ObservedArticle`として保存する。

```python
def convert_fetched_article(
    fetched: FetchedArticle,
    *,
    source: ArticleSource,
    source_id: int,
) -> AnalyzableArticle | ObservedArticle | AcquisitionConversionRejection:
    ...


class AnalyzableArticle(BaseModel):
    """必要な情報が揃い、分析工程へ進める記事。"""


class ObservedArticle(BaseModel):
    """分析にはまだ進めないが、取得時点で観測できた事実を残す型。"""
```

判断を一か所に集めたことで、ソースごとに配信形式が違っても、それを意識せずにアプリケーション内部の概念として記事を扱えるようになりました。

次は、このソースごとの違いを、どこで引き受け、扱いやすくするかに向き合っていきます。


## 6.4 ソースごとの違いを、どこに持たせるか

記事を取得する工程で最後まで悩み続けたのは、外部ソースごとの違いをどう扱うかでした。

ソースによって、最初から渡してくれる情報が大きく違います。配信に本文まで含まれることもあれば、要約だけのこともある。タイトルや公開日時を持つソースもあれば、URL の一覧しか返さないソースもあります。

しかも、その違いはこちらでコントロールできません。何を、どんな形式で配信するかは相手側の都合で決まり、本文を含めていた配信が要約だけに変わることも、HTML の構造が変わることもあります。
新しいソースを追加するたびに、そのソースの事情に合わせた個別対応が増えていきました。

だから必要だったのは、外部ソースの事情に取得工程全体が振り回されない形を作ることでした。
この形が決まるまで、ソースの定義を何度も作り直すことになります。


### 共通の概念を定義する

はじめは、その差を「外部から記事を取ってくるもの」= Fetcher という概念に閉じ込めようとしました。
ソースごとに Fetcher を用意すれば、上流はどのソースかを意識せずに済むはずだと考えていました。

```python
class Fetcher(Protocol):
    """1 ソースぶんの取得を担う入口。"""

    def fetch(self, source: NewsSource) -> AsyncIterator[FetchedArticle]: ...
```

けれど、この構造には問題がありました。

ソースごとに事情は違い、VentureBeat なら本文に RSS の長いほうを採り、Electrek なら RSS 本文は信用せず後段に回し、Hacker News なら本文を持たない投稿は捨てる、といった差があります。

しかし、どの Fetcher も、endpoint を読み、取れた項目を FetchedArticle に写す、骨格は同じです。
その共通の流れを、すべてのソースで繰り返し定義し、ソースを一つ増やすたびに書き足していました。

本来は、ソースごとの違いは加味しつつ、処理は共通にすべきでした。そう感じて、設計を変えることにしました。


### 共通の流れを切り出し、固有の事情を扱う

まずソース固有の事情を分けることにしました、
確認していくと、ソースの性質にはある程度の共通点があることに気づきます。

そこで `ArticleCompletionPolicy` を定義しました。
配信している情報の扱いについて、採用する情報、HTMLから補完する情報を優先するものなどポリシーとして定義して、
それをソースに持たせます。

```python
# 多くのソース: title と published_at は配信 (RSS) の値を信じ、body だけ HTML から取り直す。
DEFAULT_POLICY = ArticleCompletionPolicy(
    {
        CompletableField.title: FieldCompletionRule.observed_preferred,
        CompletableField.body: FieldCompletionRule.html_required,
        CompletableField.published_at: FieldCompletionRule.observed_preferred,
    }
)

# sitemap / listing 系: 配信側に本物のタイトルが無く、HTML 側を正本にする。
HTML_TITLE_POLICY = ArticleCompletionPolicy(
    {
        CompletableField.title: FieldCompletionRule.html_preferred,
        CompletableField.body: FieldCompletionRule.html_required,
        CompletableField.published_at: FieldCompletionRule.observed_preferred,
    }
)
```

次に、`SourceAdapter` を定義しました。
ソースごとの固有の事情をこの部分に定義することで、記事を取得するときに、このアダプターを使用すれば、固有の事情を吸収することができると考えました。

```python
class SourceAdapter(Protocol):
    """外部ソースごとの読み取りと、共通の取得材料への写像を担う。"""

    NAME: str
    ENDPOINT_URL: str
    completion_policy: ArticleCompletionPolicy

    def collect(self) -> AsyncIterator[FetchedArticle]: ...
```



アダプターの`collect()`を呼ぶことでソース固有の情報を加味して取得することができる
次は

だから次にやりたかったのは、この流れをソースから取り上げ、共通の関数が、ソースごとの宣言を呼び出す形にすることでした。


### ソースを宣言に寄せる

ソースは本来、「この URL を読む」「この Reader を使う」「この候補を対象にする」「この項目は信じてよい」「本文は後から補う」といった、読み方や性質を表すもののはずでした。

そこで、これまでソース側の `collect()` が進めていた取得の流れを、`fetch_articles` という共通の取得処理に移しました。
ソースは処理を実行する主体ではなく、その処理が読み取るための宣言を持つ形に寄せていきました。

ソース側に残したのは、取得の流れそのものではありません。
どの Reader を呼ぶか、どの候補を対象にするか、どの順序で採用するか、取れた値を `FetchedArticle` にどう写すか、という小さな判断だけです。

```python
class ArticleSource(Protocol[T]):
    name: ClassVar[SourceName]
    endpoint_url: ClassVar[str]
    completion_policy: ClassVar[ArticleCompletionPolicy]

    async def read(cls, tools: ReaderTools) -> list[T]: ...
    def in_scope(cls, entry: T) -> bool: ...
    def select(cls, entries: list[T]) -> list[T]: ...
    def map_entry(cls, entry: T) -> FetchedArticle: ...


async def fetch_articles(
    source: ArticleSource[T],
    tools: ReaderTools,
) -> AsyncIterator[FetchedArticle]:
    entries = await source.read(tools)

    for entry in source.select([e for e in entries if source.in_scope(e)]):
        yield source.map_entry(entry)
```
ただ、この形で答えが出たわけではありません。

ソースごとにクラスを定義する必要は本当にあるのか。
共通した読み方があるなら、ソース単位ではなく、形式や性質の分類だけを定義すればよいのではないか。
`read` や `map_entry` を持つソースは、本当に宣言と言えるのか。
取得を進める共通の概念は、関数でよいのか、それとも Fetcher という明示的な型にするべきなのか。

考え直すほど、新しい問いが出てきました。

けれど、ソースごとの差を、条件分岐として散らすのではなく、どの概念に引き受けさせるのかを決めること。
その境界を考えることが、変更に強い設計を考える入口になりました。


## 6.5 第6幕の終わりに

この幕で一番大きかったのは、正しい設計に一度でたどり着いたことではありません。むしろ、正解がはっきり見えない中で、「この処理は何をしているのか」「その責任はどこに置くべきなのか」を問い続けたことでした。

今の形が最終的な正解だと断言できるわけではありません。それでも、違和感を放置せずに、責任の境界を言葉にし直し、作り替えていく。その姿勢を持てるようになったことが、第6幕の一番大きな変化でした。

そして、第7幕では、今度は失敗の表し方そのものを測り直していきます。

次: [第7幕 — 失敗と向き合う](07-remeasuring-failure-types.md)
