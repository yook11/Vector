[← 目次](README.md) ・ 前: [第5幕](05-audit-makes-separation-real.md)

# 第6幕 — 炙り出された責務を、ドメインモデルへ解き直す

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

つまり、同じ事実が DB の中にも、キューに積まれた値の中にも存在する状態になっていたのです。
私は条件を型で表しているつもりでしたが、実際には、DB に保存された事実と Ready にコピーした値の整合性をどう保つかまでは考えられていませんでした。

そうなると、処理に使う値の正本をどちらに置くのかを決める必要があります。DB に永続化した値を正本とするなら、次の工程へ値そのものを渡すのではなく、ID だけを渡して、次の工程が DB から読み直せばよい。そう考えて、一度は ID だけを次の処理へ渡す発想に向かいました。

```python
class ReadyForEmbedding(BaseModel):
    model_config = ConfigDict(frozen=True)

    analysis_id: int = Field(gt=0)
```
次の処理では、この ID を使って DB からデータを読み直し、処理を開始するようにしました
。DB を正本にするという意味では、こちらのほうが筋が通っているように見えました。

けれど、この型を見たときに、別の違和感が残りました。
実際には ID を運んでいるだけの薄いラッパーになっていたのです。

条件を保証するための方のはずなのに、IDを運んでいるだけになった。

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

Ready を見直したことで、それまで自分が導入してきた他の型は、いったい何を保証しているのか、が気になり始めました。一つずつ見ていくと、工程と工程の境界を跨ぐところの多くで、何も保証していないのに、ただ型になっているだけの箇所が、いくつもあることに気づきました。

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

このラップされた型を受け取った後は、種類を見て中身を取り出しているだけでした。

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

それまでは境界を跨ぐ時には、型を使用する程度のことしか考えることができていなかったんだと思います。
型で何を保証するということの意味を考えることができていなかったことを痛感しました。

同じように、何も保証していないのにただ包んでいるだけの型が、ほかの工程の境界にもいくつも残っていました。


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
最初の工程では、「記事を見つけた」という記録をする。取得元、URL、タイトルだけを残し、本文や公開日時など、分析に進むために必要な情報は、後続のスクレイピング工程で補う。

この時は、この設計で問題ない、綺麗に責任がわかれていると考えていました。


### 工程自体の問題点

この設計には見落としがありました。ソースによっては、配信された時点で、すでに分析に進める記事を満たしているものがあったのです。

手順を統一していたせいで、後から改めて同じページを開きに行ったときに、 Bot 対策に阻まれ、スクレイピングそのものが失敗することがありました。配信の時点で本文が揃っていたのに、それを使わずに捨て、わざわざ取りに行き直し、失敗して、記事ごと失っていたのです。

そこで、まず考え方を変えました。

取得した時点で、タイトル、本文、公開日時、取得元、URL が揃っているなら 条件を満たしていることを表す型`ReadyForArticle` として扱い、そのまま記事として永続化して分析工程へ進める。

条件を満たさなかった場合は `PendingHtmlFetch` として扱う。取得できた値は保存し、後続のスクレイピング工程で `PendingHtmlFetch` と HTML 由来の値を合わせて、`ReadyForArticle` に昇格させる。
ここで、状態を変える判断を振る舞いとして持たせる発想も生まれました。

```python
class PendingHtmlFetch(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=500)
    source_id: int = Field(gt=0)
    source_url: SafeUrl
    published_at_hint: PublishedAt | None = None
    
class ReadyForArticle(BaseModel):
    article: FetchedArticle

    @classmethod
    def try_advance_from(
        cls,
        pending: PendingHtmlFetch,
        body: str,
        html_published_at: PublishedAt | None,
    ) -> ReadyForArticle | Failed:
   　　　# 取得時点の公開日時を優先し、なければ HTML から補う。
        published_at = pending.published_at_hint or html_published_at
        if published_at is None:
            return Failed(reason=FailureReason.published_at_missing())

        article = FetchedArticle(
            title=pending.title,
            body=body,
            published_at=published_at,
            source_id=pending.source_id,
            source_url=pending.source_url,
        )
        return cls(article=article)


FetchOutcome = ReadyForArticle | PendingHtmlFetch | Failed
```

けれど、これで話は終わりませんでした。ソースによって、配信の時点でどこまで教えてくれるかが、まったく違ったのです。


## ソースごとの違いを、どこに持たせるか

記事を取得する工程で最後まで悩み続けたのは、外部ソースごとの違いをどう扱うかでした。

ソースによって、最初から渡してくれる情報が大きく違います。配信に本文まで含まれることもあれば、要約だけのこともある。タイトルや公開日時を持つソースもあれば、URL の一覧しか返さないソースもあります。

しかも、その違いはこちらでコントロールできません。何を、どんな形式で配信するかは相手側の都合で決まり、本文を含めていた配信が要約だけに変わることも、HTML の構造が変わることもあります。
新しいソースを追加するたびに、そのソースの事情に合わせた個別対応が増えていきました。

だから必要だったのは、外部ソースの事情に取得工程全体が振り回されない形を作ることでした。
この形が決まるまで、ソースの定義を何度も作り直すことになります。


### 共通の概念を定義する

はじめは、「外部から記事を取ってくるもの」という概念を Fetcher として定義しました。
ソースごとの差は Fetcher の中に閉じ込めれば、上流の処理はどのソースから来た記事なのかを意識せずに済むはずだと考えていました。

そのため、取得方式の違いは、それぞれのソースの Fetcher が持つ `fetch` メソッドの中に書いていました。上流は `Fetcher` という同じ入口を呼び、返ってくる `FetchOutcome` だけを見る。そうすれば、ソースごとの事情を取得クラスの内側に押し込められると思っていたのです。

```python
class Fetcher(Protocol):
    """1 ソース分の取得を担う Fetcher の構造的契約。

    各 Fetcher は、RSS / HTML / API / クローラなどの取得方式の違いを
    自分の `fetch` メソッドの中に閉じ込める。

    その中では、外部へのアクセス、レスポンスの読み取り、
    記事候補への変換、使えない取得結果の棄却までを行う。

    上流はソースごとの事情を知らず、
    返ってくる `FetchOutcome` だけを見る。
    """

    PROVIDES: ClassVar[frozenset[str]]

    def fetch(self, source: NewsSource) -> AsyncIterator[FetchOutcome]: ...
```

この構造には問題がありました。ソースごとの Fetcher が、外部へアクセスする処理だけでなく、
本来は共通の流れにしたいはずの「取れた材料をどう扱うか」という判断まで、ソースごとのクラスの中に散らばっていたのです。


### アダプターを定義する

そこで次に、`SourceAdapter` という概念を定義しました。
Adapter は、外部ソースをどう読み、取れた値を `FetchedArticle` という共通の取得材料に写すところを担います。

```python
class SourceAdapter(Protocol):
    """外部 source ごとの raw 取得 + 共通言語化を担う。"""

    NAME: str
    ENDPOINT_URL: str
    completion_policy: ArticleCompletionPolicy

    def collect(self) -> AsyncIterator[FetchedArticle]: ...
```
共通の Fetcher は Adapter を呼び、返ってきた `FetchedArticle` を共通の変換処理に渡すだけになります。
そのため、ソースごとに `Ready` / `Failed` の判断を書き散らすのではなく、取得材料をどう扱うかを一か所に集められるようになりました。


### ポリシーを定義する

作り直しながら、ソースごとの差には共通した形があることに気づきました。
違っていたのは、どう取得するかよりも、取れた値のうち何を信じ、何を後から補うべきかでした。

そこで、その違いをソースごとの補完ポリシーとして定義しました。
ソースごとにポリシーを持たせれば、取れた値をどう扱うかを、個別の処理ではなく宣言に沿って判断できると考えたのです。

```python
DEFAULT_POLICY = ArticleCompletionPolicy(
    {
        CompletableField.title: FieldCompletionRule.observed_preferred,
        CompletableField.body: FieldCompletionRule.html_required,
        CompletableField.published_at: FieldCompletionRule.observed_preferred,
    }
)

HTML_TITLE_POLICY = ArticleCompletionPolicy(
    {
        CompletableField.title: FieldCompletionRule.html_preferred,
        CompletableField.body: FieldCompletionRule.html_required,
        CompletableField.published_at: FieldCompletionRule.observed_preferred,
    }
)

class FeedBasedSource:
    name = SourceName("Feed Based Source")
    completion_policy = DEFAULT_POLICY


class ListingBasedSource:
    name = SourceName("Listing Based Source")
    completion_policy = HTML_TITLE_POLICY
```

ポリシーによって、「取れた値をどう扱うか」は共通側へ寄せられました。

けれど、「外部ソースを読み、候補を選び、取得材料に写す」という流れは、まだソース側の `collect()` に残っていました。
そのため、コード上ではまだ「ソースがニュースを取得する」という不自然な主語になっていたのです。


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

けれど、この問いに向き合ったことで、変更に強いコードとは、ただ共通化したコードではないのだと分かってきました。
外部ソースの事情に振り回される部分と、内部で守りたい流れを分けること。

ソースごとの差を、条件分岐として散らすのではなく、どの概念に引き受けさせるのかを決めること。
その境界を考えることが、変更に強い設計を考える入口になりました。

残った問いは、変更に強い形にするために、記事取得という工程をどんな概念で捉え直す必要があるのか、でした。
外部ソースごとの違いを吸収しながら、内部では何を満たしたものを次の工程へ渡したいのか。
その条件を、どこで、どの言葉として定義すべきなのかを考える必要がありました。

次の節では、記事を取得する工程そのものを、どんなドメインの言葉で解き直したのかを見ていきます。


## 記事取得の概念を紐解いていく

工程を分けた時点で、記事取得にはいくつかの段階があることは見えていました。
ただしその段階は、まだ今のようなドメイン概念として整理されていたわけではありません。

最初にあったのは、外部から見つけた記事候補と、本文などを補って分析に進める記事を分ける、という処理上の分離でした。

けれど作り直していくうちに、問題は単に「完成しているか、未完成か」ではないことが見えてきました。
外部から取れた値は、まずアプリケーション内で扱える材料に変換する必要があります。
そして、その材料が分析に進めるだけの条件を満たしているかは、さらに別に判断する必要があります。

ここで初めて、取れた事実を表すものと、分析に進める保証を持つものを分けて考える必要があると分かってきました。

そこでまず、外部の形式を読む役割を Reader として分けました。
Reader は RSS や HTML を読み、そこから取れた項目の列を返します。

ただし、その項目はまだアプリケーション内の記事ではありません。
外部から読めた値を、いきなり `AnalyzableArticle` のような強い概念に入れると、欠けている値や信頼できない値を無理に扱うことになります。

そこで、アプリケーションの概念として判断する前に、一度 `FetchedArticle` という取得材料として受ける形にしました。

```python
@dataclass(frozen=True, slots=True)
class FetchedArticle:
    """外部ソースから取れた値を、まだ判断せずに共通の形へ揃えたもの。"""

    title: str
    url: str
    body: str | None
    published_at: datetime | None
```

まず、外部の形式の差を、いちばん手前で吸収します。RSS、sitemap、HTML listing、API と、ソースによって配信の形式は違います。この形式ごとの読み取りを Reader という役割に閉じ込め、読み取った結果を、形式ごとの軽い Entry にする。Entry は、まだ意味づけをしない、取れた値をそのまま写した箱です。ここでは記事かどうかも、分析に進めるかも判断しません。形式の違いだけを、ここで吸収します。

そして、その取得材料を、`convert_fetched_article` がアプリケーション内部の概念へ翻訳します。

ここでアプリケーション内部の型に変換していきます。
そのまま分析に進めるなら `AnalyzableArticle`。
後続工程で補えるように、取得時点で観測できた事実を残すなら `ObservedArticle`。
このどちらも満たすことができなければ変換エラーとして記録する。取得材料を、この三つのどれかに必ず振り分けます。

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

判断を一か所に集めたことで、ソースごとに揃う情報が違っても、アプリケーションの概念へ翻訳する処理は散らばらなくなりました。

Reader は外部形式を読み、FetchedArticle は取得できた材料を受け止める。
その材料を見て、分析に進めるものは AnalyzableArticle にし、まだ補完が必要なものは ObservedArticle として残す。

このように記事取得の工程を捉え直したことで、外部ソースの都合に引きずられにくい設計に少し近づけたと思います。

これで完璧になったとは思っていません。
ただ、正解らしきものを急いで決めるのではなく、実装の違和感に向き合いながら概念を見直していくことは、これからも続けていきたいです。


## 6.5 第6幕の終わりに

この幕で一番大きかったのは、正しい設計に一度でたどり着いたことではありません。むしろ、正解がはっきり見えない中で、「この処理は何をしているのか」「その責任はどこに置くべきなのか」を問い続けたことでした。
Ready は何を保証するのか。「完成させる」とは、実際には何をしているのか。ソースは記事を取りに行く主体なのか、それとも読み方を表すものなのか。そうした問いを立て直すたびに、名前も型も置き場所も変わっていきました。
今の形が最終的な正解だと断言できるわけではありません。それでも、違和感をそのまま流さず、責任の境界を言葉にし直し、テストで確かめながら作り替えていく。その姿勢を持てるようになったことが、第6幕の一番大きな変化でした。

そして、その問いは失敗の型にも向かっていきます。失敗を値にするだけで十分なのか。その値は何を語るべきなのか。第7幕では、今度は失敗の表し方そのものを測り直していきます。

次: [第7幕 — 失敗の型を、測り直す](07-remeasuring-failure-types.md)
