[← 目次](README.md) ・ 前: [第5幕](05-audit-makes-separation-real.md)

# 第6幕 — アプリケーションの概念と向き合う

第6幕では、今まで足りていなかった、アプリケーションの中の概念と向き合うことになります。

中心となるのは、AIの提案を受けながら作ってきた型や抽象を、「この型は何を保証するのか？」と問い直し、自分自身の理解に基づいて設計し直していくことです。
なお、この時期は語彙の統一や改名、設計の見直しが並行して進んでいたため、ここで扱う内容には現在の設計と異なる部分も多くあります。


## 6.1 保証するべきこととは？

第3幕では、型は単にデータの形を表すだけでなく、満たすべき条件まで表現できるものだと知りました。

4月末には、この考え方を非同期パイプラインにも応用しようとしていました。パイプラインでは、記事をある工程から次の工程へ渡すために、あらかじめ満たしておくべき条件があります。そこで、その条件を `ReadyFor{ステージ名}`（記事抽出なら `ReadyForExtraction`）という型で表現すれば、次の工程へ進める記事を明確に区別でき、より堅牢な設計にできると考えました。以降、この一連の型をまとめて Ready 型と呼びます。

上流のタスクは、直前の工程で得た結果とDB上の現在の状態をもとに、次の工程へ進めるかを判定する。条件を満たしていれば Ready 型を構築し、次のタスクへ渡す。
そうすれば、下流のタスクはその型を受け取った時点で、必要な前提条件はすでに満たされているものとして処理を開始できるはずだ、と考えていたのです。

たとえば、取得した記事をAIによる情報抽出に渡す前には、タイトルと本文が揃い、本文が上限を超えていないことを型の構築時に確認していました。さらにDBを参照し、その記事の抽出結果がまだ作成されていないことを確認します。すべての条件を満たした場合だけ Ready 型を作り、その型自体をキューへ投入していました。


```python
class ReadyForExtraction(BaseModel):
    article_id: int = Field(gt=0)
    original_title: str = Field(min_length=1)
    original_content: str = Field(
        min_length=1,
        max_length=200_000,
    )

...
# すでに抽出済みの記事は、次の工程へ進めない
if await extraction_repo.exists_for_article(article_id):
    return None

return ReadyForExtraction(
    article_id=article_id,
    original_title=original_title,
    original_content=original_content,
)
```

しかしこの設計には問題がありました。

データの流れを改めて追うと、同じ値が二つの場所に存在していることに気づきました。処理が終わるとDBに永続化されます。一方、次のタスクに渡る値は、処理の結果を使用してそのまま Ready 型として構築されます。

キューへ投入した時点では両者は同じです。しかしこの設計は、その後も両者が同じであり続けることを、どこでも保証していません。次のタスクが実行されるまでにDBの状態が変われば、両者は一致しなくなります。

型が保証していたのは Ready 型を構築した時点の条件だけであり、その後もDBに保存された事実と一致し続けることまでは、保証できていなかったのです。

これを避けるには、処理に使う値の正本をどこに置くのかを決める必要があります。

私は、DBに永続化された値を正本とすることに決めました。正本がDBにあるなら、次の工程へデータそのものを渡す必要はありません。
永続化した値を指すIDだけを渡し、次のタスクは実行時にDBから読み直せばよい。そう考えて、Ready 型をそのまま渡すのではなく、IDだけを次のタスクへ渡す設計へ、いったん向かいました。

```python
class ReadyForExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    article_id: int = Field(gt=0)
```

けれど、IDだけを持つその型を見たとき、別の違和感が残りました。次の工程へ進める条件を表すはずの Ready 型が、実際にはIDを運ぶだけの薄いラッパーになっていたからです。
そのIDが指すデータが本当に処理可能な状態にあるかは、型からは分かりません。それでも Ready と呼ぶのは、名前と役割が一致していません。

そこで、前の工程がキューへ投入する時点で「次へ進める」と保証するのではなく、次の工程自身が処理の開始直前にDBの状態を確認し、その時点の条件を満たしている場合だけ Ready 型を構築する設計が良いのではないかと考えました。

```python
# 上流が渡すのは、対象記事を指すIDだけ
class ExtractionTrigger(BaseModel):
    model_config = ConfigDict(frozen=True)

    article_id: int = Field(gt=0)
```

下流タスクが、開始直前にDBを読み直してから Ready を構築します。

```python
async def extract_content(trigger: ExtractionTrigger) -> None:
    # 開始直前に、DBの「いまの」状態を読み直す
    article = await article_repo.get(trigger.article_id)

    # まだ抽出しておらず、本文が上限内 —— いま処理できる時だけ Ready を組む
    if article.already_extracted or len(article.content) > MAX_CONTENT_LENGTH:
        return  # 条件を満たさなければ、ここで打ち切る

    ready = ReadyForExtraction(
        article_id=article.id,
        original_title=article.title,
        original_content=article.content,
    )
    ...
```

型を定義するときは、まず「どの境界で、どの条件が満たされていなければならないか」をはっきりさせ、その条件を構築時に確認できる形にする。
守りたい条件が曖昧なまま型を作っても、名前が強いだけで、実際には何も保証できません。型の設計は、そこから始める必要があると学びました。


## 6.2 意図のない型を見直していく

Ready型を見直したことをきっかけに、これまで自分が導入してきたほかの型にも問題があるのではないかと考えました。
一つひとつ確認していくと、工程間の境界で使われている型の多くが、実質的には何の意味もないことに気づきました。

例えばAI分析は、記事を「分析対象（in-scope）」と「対象外（out-of-scope）」のどちらかに振り分けます。


分析対象と判定された記事は `InScopeAssessment`。
翻訳タイトル・要約・投資家向けの示唆が揃い、それぞれが空でないことを、構築時に確認し、次の工程に進む条件を型で保証します。

対象外と判定された記事は `OutOfScopeAssessment`。これは次の工程には進まないため型を分けて設計していました。

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


# out-of-scope と判定された記録。
@dataclass(frozen=True)
class OutOfScopeAssessment:
    ...
```

結果の型も、本来はその二つ——`InScopeAssessment` か、`OutOfScopeAssessment`——のどちらかが返ればよいはずでした。
ところが、AI処理を実行している部分はこの型をそのまま返さず、もう一段ラップした型で返していました。

```python
# 評価結果を、もう一段ラップしただけの型。
@dataclass(frozen=True)
class InScopeOutcome:
    assessment: InScopeAssessment

AssessmentOutcome = InScopeOutcome | OutOfScopeOutcome
```

このラップされた型は、受け取った後に特別な分岐もなく、種類を見て中身を取り出すだけでした。
ここでも、型で何を保証すべきかを、まだ考えられていなかったのだと思います。

実際、この型を消しても処理は一行も変わりませんでした。この型は、何も保証していなかったのです。

Before —
```python
# ラッパーから中身を取り出すだけ。分岐は種類を見ているだけで、特別な処理はない。
if isinstance(result, InScopeOutcome):
    ready = ReadyForEmbedding.from_assessment(result.assessment, ...)
```

After —
```python
# ラッパーを外し、評価結果そのものを返すようにした。
async def execute(...) -> InScopeAssessment | OutOfScopeAssessment:
    ...

if isinstance(result, InScopeAssessment):
    ready = ReadyForEmbedding.from_assessment(result, ...)  # result.assessment → result
```

同じように、何も保証していないのにただ包んでいるだけの型が、ほかの工程の境界にもいくつも残っていました。

それまで境界を跨ぐ時には、型を使用する程度のことしか考えることができていなかったんだと思います。

ここでも、自分がAIの提案を鵜呑みにして、型で何を保証するのかという意味を考えることができていなかったことを痛感しました。


## 6.3 ニュースを取得する工程に目を向ける

これまで導入してきた型を見直すなかで、「分析に進めるだけの品質を備えた記事とは何か」「そのために、どのような条件を保証すべきか」についても、あらためて考えることにしました。

第3幕ではすでに、記事を「最初に見つけた段階」と「分析に進める段階」に分けて整理していました。

最初の段階で保存するのは、記事の URL とタイトルだけです。この時点では、まだ分析対象ではありません。
そのあと、保存した URL にアクセスして本文を取得し、articles の行として保存できたものだけを、分析対象としていました。

分析に進めることを、DB に対応する行が存在することによって表していましたが、工程の出口として「どんな条件を満たせば分析に進めるのか」は、まだはっきりした型になっていませんでした。

そこで、これまでと同じように、工程の出口で満たすべき条件を型として定義することにしました。
分析に進めるには、タイトルと本文が揃っていること、本文が短すぎず長すぎないこと、配信日時とソースが分かること、そして URL が安全に扱える形であること。
これらを、取得工程を通過した記事の条件として FetchedArticle に定義しました。

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

満たすべき条件に向き合っていく中で、この工程そのものの問題点に気づくことになります。

この設計は、もともと「配信された情報だけで分析を始めようとすると足りない」という問題を、スクレイピングで補うという発想から生まれたものでした。
まず記事の URL を「記事を見つけた」事実として記録し、そこから本文を取得しにいく。取得工程を、URL を記録する最初の工程と、後続のスクレイピング工程の二段に分けたのです。

まず基礎的な情報を集め、後から分析に進められるようにする。当時はこれで責任が綺麗に分かれ、うまく設計できていると考えていました。

## 工程自体の問題点

しかし、この設計には見落としがありました。ソースによっては、配信されたデータに本文まで含まれており、その時点ですでに分析に進める条件を満たしていたのです。

それにもかかわらず、処理手順を統一していたため、配信された本文を使わず、あとから URL を開いて取得し直していました。
その結果、Bot 対策に阻まれてスクレイピングに失敗し、配信の時点で本文が揃っていたのに、それを使わずに捨て、記事そのものを分析対象から失うことがありました。

ここで、取得した記事にも複数の状態があることに気づきました。

そこで、取得後の処理を分けることにしました。配信時点で分析に必要な情報が揃っている記事は、そのまま分析へ進めます。
一方、情報が不足している記事は、取得できた情報をいったん保存し、あとから HTML を取得して本文を補う工程へ回します。


しかし、これでは不十分でした。ソースごとに配信形式が異なり、同じ情報でもフィールド名や格納場所、欠損の表され方が揃っていないからです。
こうした値を外部の形式のままアプリケーションに渡すと、後続の処理がソースごとの違いを意識しなければなりません。

その結果、本来は取得した記事の品質だけを見て「分析に進めるか」を判断したいはずが、どこから取得をしたのかを確認する必要が出てきます。
新しいソースを追加するたびに判断ロジックへ分岐が増え、型も「どのような記事であるか」ではなく、「どのソースから来たか」に引きずられてしまいます。

つまり、これまでの工程には、外部の配信形式とアプリケーション内部の概念を分けて考えるための役割が足りていませんでした。
そこで、取得の処理そのものを「ソース固有の形式を読む」「アプリケーション共通の形に揃える」「分析に進める条件を満たしているか判断する」という三つの役割に分けました。

まず、外部からソース固有の形式を読み取る役割として、Reader という概念を導入しました。
Reader は、RSS、sitemap、HTML listing、API など、それぞれの配信形式を解釈し、取得した値を形式ごとの軽量な Entry として返します。

```python
class RssReader:
    """RSS フィードを取得し、RssEntry の列として返す。"""

    async def fetch(self, *, endpoint_url: str, source_name: str) -> list[RssEntry]:
        ...


class HtmlListingReader:
    """HTML 一覧ページを取得し、HtmlListingEntry の列として返す。"""

    async def fetch(self, *, url: str, source_name: str) -> list[HtmlListingEntry]:
        ...
```

Entry は、配信された値のうちアプリケーションが使うものを、その形式のまま写した型です。
表記の軽い整え（タグ除去や日時の解釈）はしますが、値に意味を与えたり、記事として成立するか・分析に進めるかを判断したりはしません。
共通の形に揃えるのは次の FetchedArticle の役割で、Entry はそこまで踏み込みません。
ソース固有の違いをこの境界に閉じ込め、後続の処理へ直接持ち込まないことが、Reader と Entry の役割です。

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

この Entry を、アプリケーション内部の概念へ翻訳する前に、まず形式の差を落とした共通の中間型 FetchedArticle にまとめます。

```python
@dataclass(frozen=True, slots=True)
class FetchedArticle:
    """外部ソースから取れた値を、まだ判断せずに共通の形へ揃えたもの。"""

    title: str
    url: str
    body: str | None
    published_at: datetime | None
```

そして、convert_fetched_article が、この共通中間型をアプリケーション内部の概念へ翻訳します。
そのまま分析に進めるなら `AnalyzableArticle`、まだ進めなければ、後続工程で補えるように `ObservedArticle` として保存します。

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
    """分析にはまだ進めないが、皇族工程で補うため取得したデータを残す型。"""
```

こうして、外部ソースごとの配信形式の違いを吸収する設計になったと考えています。
「形式を読む」「共通の形に揃える」「分析に進めるか判断する」と役割を分けたことで、後続の処理はソースの違いを意識せず、アプリケーション内部の概念として記事を扱えます。
それぞれの型が何を引き受けるのかを考えることが重要だと学びました。


## 6.4 ソース固有の事情を、どこに閉じ込めるか

記事を取得する工程で最後まで悩み続けたのは、外部ソースごとの違いを、どこに定義して持たせるかでした。

ソースによって、最初から渡してくれる情報は大きく違います。配信に本文まで含まれることもあれば、要約だけのこともあります。タイトルや公開日時を持つソースもあれば、URL の一覧しか返さないソースもあります。

しかも、その違いはこちらでコントロールできません。何を、どんな形式で配信するかは相手側の都合で決まり、本文を配信していたのに要約だけに変わることも、HTML の構造が変わることもあります。新しいソースを追加するたびに、そのソースの事情に合わせた個別対応が増えていきました。

だから必要だったのは、外部ソースの事情に取得工程全体が振り回されない形を作ることでした。
この形が決まるまで、ソースの定義を何度も作り直すことになります。


## 共通の概念を定義する

はじめは、その差を Fetcher——「外部から記事を取ってくるもの」——という概念に閉じ込めようとしました。
ソースごとに Fetcher を用意すれば、上流はどのソースかを意識せずに済むはずだと考えていました。

```python
class Fetcher(Protocol):
    """1 ソースぶんの取得を担う入口。"""

    def fetch(self, source: NewsSource) -> AsyncIterator[FetchedArticle]: ...
```

けれど、この構造には問題がありました。

確かに、ソースごとに事情は違います。配信された本文をそのまま使えるソースもあれば、RSS の本文が不完全で、後段のスクレイピングで取り直したいソースもあります。本文の候補が複数あって長いほうを採る、本文を持たない投稿は捨てる、といった細かな判断も、ソースごとに変わります。

しかし、どの Fetcher も、endpoint を読み、取れた項目を FetchedArticle に写す、という目的は同じです。
それなのに、こうしたソースごとの差を表すためだけに、ソースの数だけ Fetcher を定義していました。

本来は、ソースごとの違いは加味しつつ、処理は共通にすべきでした。そう感じて、設計を変えることにしました。


### 共通の流れを切り出し、固有の事情を扱う

まず、ソース固有の事情を整理することにしました。確認していくと、ソースの性質には、ある程度の共通点があることに気づきました。

そこで、その共通点をポリシーとして切り出し、ソースごとに持たせればよいのではないか——そう考えて、`ArticleCompletionPolicy` を定義しました。
配信された情報のうちどれを採用するか、本文を HTML からの補完で補い、どちらを優先するか。そうした情報の扱いの方針をポリシーにまとめ、それをソースに持たせます。

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

次に、ソースごとの事情を一か所に閉じ込める場所として、SourceAdapter という概念を定義しました。
ソース固有の事情をこのアダプターに書いておけば、記事を取得するときは、アダプターを通すだけでその違いを吸収できる——そう考えました。

アダプターの`collect()`を呼ぶことでソース固有の情報を加味して取得することができます。

```python
class SourceAdapter(Protocol):
    """外部ソースごとの読み取りと、共通の取得材料への写像を担う。"""

    NAME: str
    ENDPOINT_URL: str
    completion_policy: ArticleCompletionPolicy

    def collect(self) -> AsyncIterator[FetchedArticle]: ...


# 共通の実行役がアダプターを受け取り、collect を実行する
class ArticleFetcher:
    def __init__(self, adapter: SourceAdapter) -> None:
        self._adapter = adapter

    async def run(self) -> AsyncIterator[FetchedArticle]:
        async for article in self._adapter.collect():
            yield article
```

けれど、この形にも違和感が残りました。ソース固有の事情を閉じ込めるための `SourceAdapter` が、いつのまにか `collect()` で取得を進める主役になっていたのです。
本来、ソース固有の事情は、それぞれのソースクラスを定義して、自身が持つべきものではないか——そう考えました。

ソースは本来、「この URL を読む」「この Reader を使う」「この候補を対象にする」「この項目は信じてよい」「本文は後から補う」といった、読み方や性質を宣言するものであって、処理を実行する主体ではないはずです。

そこで、これまでアダプターの collect() が進めていた取得の流れを、fetch_articles という共通の取得処理に移しました。
ソースは処理を実行する主体ではなく、その処理が読み取るための宣言を持つ形に寄せていきます。

ソースに残したのは、取得の流れそのものではありません。
どの Reader を呼ぶか、どの候補を対象にするか、どの順序で採用するか、取れた値を `FetchedArticle` にどう写すか、という小さな判断だけです。

```python
class ArticleSource(Protocol[T]):
    name: ClassVar[SourceName]
    endpoint_url: ClassVar[str]
    completion_policy: ClassVar[ArticleCompletionPolicy]

    # どの Reader を呼ぶか
    async def read(cls, tools: ReaderTools) -> list[T]: ...
    # その entry を収集対象に含めるか
    def in_scope(cls, entry: T) -> bool: ...
    # 採用する entry を絞り込む
    def select(cls, entries: list[T]) -> list[T]: ...
    # entry を FetchedArticle に変換する
    def map_entry(cls, entry: T) -> FetchedArticle: ...


async def fetch_articles(
    source: ArticleSource[T],
    tools: ReaderTools,
) -> AsyncIterator[FetchedArticle]:
    entries = await source.read(tools)

    for entry in source.select([e for e in entries if source.in_scope(e)]):
        yield source.map_entry(entry)
```


## 6.5 第6幕の終わりに

このソースごとの違いについては今でも疑問が残っています。

ソースを「宣言」に寄せるという方向そのものは、悪くなかったと思います。
ソースが自分の 事情 や 補完方針を持ち、取得の流れは共通の処理（`fetch_articles`）に任せる。その分担で、設計は見通しよくなりました。

ただし、改めて fetch_articles を見ると、やっていることはこれだけです。
宣言を見て処理が分かれる、というより、ただソースが持っているメソッドを呼んでいるだけ。
ソースクラスに固有の事情を「宣言」させることができていないのではないか——その違和感は、まだ残っています。

```python
async def fetch_articles[T](source, tools):
    entries = await source.read(tools)
    for entry in source.select([e for e in entries if source.in_scope(e)]):
        yield source.map_entry(entry)
```

でもこの幕で大きかったのは、正解がはっきり見えない中で、「この処理は何をしているのか」「その責任はどこに置くべきなのか」を問い続けたことだと思います。

今の形が最終的な正解だと断言できるわけではありません。それでも、違和感を放置せずに、作り替えていく。その姿勢を持てるようになったことが、第6幕の一番大きな変化でした。

アプリケーションの概念と向き合う中で、自分がこれまで「失敗」をきちんと定義できていなかったことに気づきました。
第7幕では、その失敗の表し方そのものを測り直していきます。

次: [第7幕 — 失敗と向き合う](07-remeasuring-failure-types.md)
