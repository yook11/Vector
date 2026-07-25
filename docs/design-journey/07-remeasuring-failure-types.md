[← 目次](README.md) ・ 前: [第6幕](06-domain-model-rebuild.md)

# 第7幕 — 失敗と向き合う

第6幕でこのアプリケーションのドメインや概念に向き合うなかで、自分たちの中にある「失敗」の定義が、まだ決定的に不十分だったことに気づかされました。

第5幕において、後から出来事を追跡できるよう監査ログを導入しました。当時はそれによって「失敗を記録できている」つもりになっていたのです。

しかし、アプリケーションへの理解が深まるにつれて、その記録の限界が見えてきました。どの工程で失敗したかは分かっても、「具体的に何が起き、なぜ失敗したのか」 という本質的な理由まで辿ることができない状況だったのです。

これでは、「後から出来事を振り返り、その経緯を正しく理解する」という監査ログ本来の目的を果たせているとは言えません。

だからこそ本幕では、各工程における「失敗」をあらためて見直します。ただ失敗した場所を特定するだけでなく、そこで何が起きたのかという『理由』までを後から読み解ける形へと、失敗の定義を再構築していきます。


## 7.1 起きた場所でしかわからないこと

「何が起きたのか」を十分に記録できていないことに最初に気づかされたのは、パイプラインの入口にあたる Reader でした。

Reader の役割は、外部ソースから取得したレスポンスを解析し、アプリケーションが必要とする値だけを抽出して Entry（取得済み記事データ）へ写すことです。

```python
@dataclass(frozen=True)
class HackerNewsEntry:
    url: str | None
    title: str | None
    published: datetime | None
    raw_created_at: str | None
```

Reader は、外部ソースごとに異なるレスポンス形式の違いをこの境界で吸収します。

ここで発生するのは、「受け取ったレスポンスを Reader が期待する構造として読み取れない」という失敗です。
その内容には、レスポンス自体が空であるケース、JSON や XML の構文が壊れているケース、あるいは相手側の配信仕様が変わり、期待していたフィールドが存在しないケースなど、まったく性質の異なるトラブルが含まれています。

しかし当時の実装では、こうした多様な失敗の原因が、すべて `read_unreadable_response` というたった一つのエラーコードへ潰されていました。

その結果、「Reader が読み取りに失敗した」という事実しか分からず、「具体的に何が原因で読めなかったのか」 が完全に隠れてしまっていたのです。

```python
class UnreadableResponseError(Exception):
    """応答を受け取ったが reader が構造化できなかった """
    CODE: ClassVar[str] = "read_unreadable_response"   # これ以上のことは読み取れない
```

そこで、読取失敗には Reader が検知した理由を持たせるようにしました。
起こりうる失敗を `reason` として定義します。

```python
class UnreadableResponseReason(StrEnum):
    EMPTY_BODY = "read_empty_body"  # 応答本文が空だった
    MALFORMED_CONTENT = "read_malformed_content"  # JSON / XML として壊れていた
    UNEXPECTED_ROOT_SHAPE = "read_unexpected_root_shape"  # 応答全体の形が違った
    UNEXPECTED_FIELD_SHAPE = "read_unexpected_field_shape"  # 必要な項目の形が違った


class UnreadableResponseError(Exception):
    def __init__(
        self,
        *,
        # 後から、読み取れなかった具体的な理由を判別できるようにする。
        reason: UnreadableResponseReason,
        response_format: str,
        field: str | None = None,
        parser_position: str | None = None,
    ) -> None:
        self.reason = reason
        self.response_format = response_format
        self.field = field
        self.parser_position = parser_position

    @property
    def CODE(self) -> str:
        return self.reason.value


raise UnreadableResponseError(
    reason=UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
    response_format="json",
    field="hits",
)
```


## 7.2 変換できなかった理由

このアプリケーションでは、AI 分析へ進むために必要な条件をすべて満たした記事を `AnalyzableArticle` という型で表現しています。

また、記事がその条件を満たせず、AnalyzableArticle へ昇格できなかったことを、`QualityTooLow` という概念で表していました。

```python
class AnalyzableArticle(BaseModel):
    model_config = ConfigDict(frozen=True)

    # タイトルは必須で、1〜500文字
    title: str = Field(min_length=1, max_length=500)

    # 本文は必須で、50〜1,048,576文字
    body: str = Field(min_length=50, max_length=1_048_576)

    # 公開日時は必須
    published_at: PublishedAt

    # 取得元を識別できる正のID
    source_id: int = Field(gt=0)

    # 正規化され、安全性を検証した記事URL
    source_url: CanonicalArticleUrl


# AI分析へ進めるだけの品質に届かなかったことを表す。
@dataclass(frozen=True, slots=True)
class QualityTooLow:
    error_class: str
    error_message: str
```

取得したデータから `AnalyzableArticle` を構築する責務は、`build_or_reject()` という関数が担っていました。 必要な基準を満たしていれば `AnalyzableArticle` を返し、満たせなければ `QualityTooLow` を返すという設計です。

分析品質を向上させ、アプリケーションを改善していくためには「どの基準を満たせずに昇格できなかったのか」を知ることが不可欠だと考えていました。

しかし、ここでもまったく同じ問題が起きていたのです。`AnalyzableArticle` になれなかった具体的な理由が、すべて `completion_invariant_rejected（不変条件の不成立）` というたった一つの曖昧な言葉へ潰されてしまっていました。

```python
@classmethod
def build_or_reject(
    cls,
    *,
    title: str | None,
    body: str | None,
    published_at: PublishedAt,
    source_id: int,
    source_url: CanonicalArticleUrl,
) -> Self | QualityTooLow:
    try:
        return cls(
            title=title,
            body=body,
            published_at=published_at,
            source_id=source_id,
            source_url=source_url,
        )
    except ValueError as error:
        return QualityTooLow(
            error_class=type(error).__name__,
            error_message=str(error),
        )

@classmethod
def from_quality_too_low(
    cls,
    quality: QualityTooLow,
) -> "CompletionRejection":
    return cls(
        # どの条件に違反しても同じ結果になる
        reason_code="completion_invariant_rejected",
        error_class=quality.error_class,
        error_message=quality.error_message,
    )
```

ただし、この工程における課題は、先ほどの Reader での失敗とは少し事情が異なっていました。

当時の `QualityTooLow` は、`Pydantic` が発生させた生のエラーメッセージを保持していました。そのため、ログを確認すれば、「どのフィールドの検証で落ちたのか」を推測すること自体は可能だったのです。単発のデバッグ調査であれば、それだけでも十分だったかもしれません。

しかし、監査ログとして残したかったのは、「このアプリケーションのどの工程で、どの不変条件を満たせず、どのような結果に至ったのか」という構造化された事実 です。

その上 `Pydantic` の検証メッセージをそのまま保存することには、大きな問題があることの気づきました：

- 集計・検索ができない: 自由文テキストのままでは、失敗理由ごとの集計（GROUP BY）やフィルタリングができません。
- データ漏洩と欠損のリスク: 生データがメッセージに混入する恐れがあり、ログの文字数制限で途中切断されると情報が失われます。
- 外部ライブラリへの密結合: Pydantic のメッセージ表現変更という外部の都合に、システムの監査ログが振り回されてしまいます。

そこで、「値の検証自体は Pydantic に任せつつ、その結果はアプリケーションで定義したものへ翻訳する」 という設計へ切り替えました。
`AnalyzableArticle` へ昇格できなかった理由を、システムが自ら管理する「どの不変条件を満たせなかったのか」というコードとして再定義しました。


```python
class AnalyzableArticleDefect(StrEnum):
    TITLE_MISSING = "analyzable_article_title_missing"  # タイトルがない
    BODY_MISSING = "analyzable_article_body_missing"  # 本文がない
    BODY_TOO_SHORT = "analyzable_article_body_too_short"  # 本文が短すぎる
    PUBLISHED_AT_MISSING = "analyzable_article_published_at_missing"  # 公開日時がない


@dataclass(frozen=True)
class QualityTooLow:
    defects: tuple[AnalyzableArticleDefect, ...]
```

修正後は、なぜ失敗したのかを、以下のように表せるようになりました。

```python
@classmethod
def from_quality_too_low(cls, quality: QualityTooLow) -> Self:
    """domain の構築拒否を completion rejection に翻訳する。"""
    return cls(
        defects=quality.defects,
        unmapped=quality.unmapped,
    )

built = AnalyzableArticle.build_or_reject(
    title=resolved.title,
    body=resolved.body,
    published_at=resolved.published_at,
    source_id=source_id,
    source_url=source_url,
)

if isinstance(built, QualityTooLow):
    return CompletionRejection.from_quality_too_low(built)
```


### AIの応答も可視化する

同様の課題は、AIを活用した工程の出力検証でも発生していました。
実際に運用してわかったのですが、AIの応答は必ずしも指定したスキーマ（出力フォーマット）に従うとは限りません。
だからこそ、改善のためには「なぜ失敗したのか」という詳細な情報が不可欠です。
そこで本工程でも、期待とどこが異なっていたのか「失敗の理由」を記録できるようにしました。

```python
class AssessmentResponseInvalidError(AssessmentRecoverableError):
    def __init__(self, defect: StrEnum) -> None:
        super().__init__(code=defect.value, provider_error=None)

# defect:の定義
class AssessmentResponseDefect(StrEnum):
    CATEGORY_KEY_MISSING = "assessment_response_category_key_missing"
    INVESTOR_TAKE_KEY_MISSING = "assessment_response_investor_take_key_missing"
    CATEGORY_WRONG_TYPE = "assessment_response_category_wrong_type"
    CATEGORY_UNKNOWN_VALUE = "assessment_response_category_unknown_value"
```


## 7.3 想定内の失敗にも理由を持たせる

取得したばかりのニュース記事は、本文が空だったり公開日時が取得できなかったりと、最初から分析に必要な情報が揃っているケースはそれほど多くありません。
外部から記事を取得する工程では、まだ分析に進めない状態の記事を `ObservedArticle` という概念（モデル）で表現していました。

この段階での情報不足は「想定内の挙動」です。そのため、他の工程のように例外（Exception）を投げるのではなく、通常の if 文による条件分岐として実装していました。

しかし当時の実装では、「例外が発生したときに記録を残す」という意識にとどまっていました。
その結果、通常の条件分岐で弾かれた場合に「なぜその分岐に入ったのか（分析に進めなかった理由）」まで記録するという発想が抜けてしまっており、すべて単に `None` を返すような実装になっていました。

```python
def convert_fetched_article(
    fetched: FetchedArticle,
) -> AnalyzableArticle | ObservedArticle | None:
    title = fetched.title.strip()[:ARTICLE_TITLE_MAX_LENGTH]
    if not title:
        return None  # title が無い

    if not fetched.url:
        return None  # URL が無い

    try:
        source_url = CanonicalArticleUrl(fetched.url)
    except ValueError:
        return None  # URL を正規化できない

    # 分析に進める品質があれば AnalyzableArticle
    ...

    try:
        return ObservedArticle(...)
    except ValueError:
        return None  # ObservedArticle すら組み立てられない

    ...
```

この工程を改めて捉え直したことで、「タイトル」や「URL」を持たないニュースソースは本来存在しない、という当たり前の事実に気づきました。
これまでは `ObservedArticle` の構築失敗が何を意味するのか、十分に整理できていなかったのです。

そもそも `ObservedArticle` は、本文や公開日時が不足していても、後続のスクレイピング工程で補完できるようにデータを保持するための型です。
エントリーを ObservedArticle として保持・後工程へ引き渡すには、以下の条件を満たす必要があるのではないかと考えました。

- URL（必須）: 後工程でのアクセスおよび識別における最低前提条件。
- タイトル（フォールバック）: 後続のHTML解析による補完工程でも取得できなかった場合の最終フォールバック。

これらを双方とも満たせないエントリーは、ObservedArticle として成立しないため棄却対象（AcquisitionConversionDefect）とすることにしました。

想定内の失敗を表すために、Pythonの例外（Exception）で処理を中断させるのではなく、通常の戻り値（AcquisitionConversionRejection）として表現しています。
これにより、1件のデータ異常でパイプライン全体を停止させず、安全に処理を継続できます。


```python
class AcquisitionConversionDefect(StrEnum):
    URL_MISSING = "acquisition_conversion_url_missing"
    # URLがなく、後続スクレイピングを実行できない

    TITLE_MISSING = "acquisition_conversion_title_missing"
    # titleがなく、ObservedArticleとしても残せない

    UNEXPECTED_ERROR = "acquisition_conversion_unexpected_error"
    # 通常は起きないはずの変換中のバグ


def convert_fetched_article(
    fetched: FetchedArticle,
) -> AnalyzableArticle | ObservedArticle | AcquisitionConversionRejection:
    if not fetched.title:
        # 捨てるのではなく、「なぜ変換できなかったか」を残す
        return _reject(reason=AcquisitionConversionDefect.TITLE_MISSING)

    # 分析に進める品質があれば AnalyzableArticle
    article = AnalyzableArticle.try_build(...)
    if article is not None:
        return article

    # 分析には進めないが、取れた事実は ObservedArticle として残す
    return ObservedArticle.build(...)
```

呼び出し側で、想定外のバグだけを例外として受けます.

```python
try:
    outcome = convert_fetched_article(fetched)
except Exception as exc:
    # 例外として受け、UNEXPECTED_ERROR + stack trace で残す。
    outcome = unexpected_rejection(fetched, cause=exc)
```

失敗の条件と意味を明確化することで、アプリケーションの理解が深まりました。
また今回のことで、「システムが記録・観察すべき出来事とは何か」を改めて見つめ直す機会にもなりました。


## 7.4 想定内のエラーにも種類がある

収集工程で「想定内の失敗」と「想定外のバグ」を整理したことをきっかけに、他工程でも境界の見直しを行いました。

その中で見つかったのが、AI分析工程におけるカテゴリ解決のエラーです。

この工程では、アプリケーション側で定義した Enum を選択肢としてAIに渡し、AIが選んだカテゴリを DB上のマスタテーブル へ照合（解決）する構造をとっています。

当初、このDBへの照合に失敗した時は「AIがカタログに存在しないカテゴリを返した『想定内の失敗』」として定義していました。

```python
class AssessmentCategoryMissingError(AssessmentTerminalError):
    """AI が catalog に存在しない category slug を返した。"""
    ...
```

しかし、AI応答のカテゴリは手前のパース段階で Enum の値であることが保証されており、「AIがカタログ外を返す」というパスは構造上あり得ませんでした。

たとえば、当時実際に追加した `mobility` カテゴリを題材にすると、この違いがよく分かります。
AIが `category="mobility"` を返すと、パース処理はまず `ValidCategory("mobility")` を構築し、さらに `InScopeCategory` へ変換していました。

```python
category = ValidCategory(category_raw)

return InScope(
    category=InScopeCategory(category.value),
    ...
)
```

`"automotive"` のような Enum に存在しない値であれば、`ValidCategory` の構築時に `CATEGORY_UNKNOWN_VALUE` として弾かれるため、DBへの照合処理まで到達しません。

一方、`mobility` は Enum の正規な値なので、このパースを通過します。
それにもかかわらず、後続の `SELECT ... WHERE slug = 'mobility'` でカテゴリが見つからないとすれば、原因はAIの応答ではありません。
アプリケーションへ `MOBILITY = "mobility"` を追加した一方で、DBマスタへ `mobility` を追加する migration を作り忘れた、または適用できていない状態です。

つまり、DBへの照合に失敗する原因は、「AIがカタログ外の値を返したこと」ではなく、EnumとDBマスタが同期されていないことにありました。

この部分は、最初から原因のラベル付けが丸ごと誤っていました。

解決できない原因は AI ではなくシステム側のバグであるため、想定内の失敗として扱うのをやめ、「想定外のバグ」として明示的に投げる例外 へ修正しました。

```python
class CategoryEnumDatabaseMismatchError(Exception):
    """アプリ側 enum と DB categories が食い違う、不変条件の破れ。

    意図的に 想定内のエラーが属す marker 階層の外に置く。
    """

    def __init__(self, missing: set[str]) -> None:
        self.missing = missing
        ...


# 解決できないのは AI のせいではない。想定内の失敗ではなく、バグとして投げる。
if category_id is None:
    raise CategoryEnumDatabaseMismatchError({in_scope.category.value})
```

本システムでは、工程ごとに発生し得るトラブル（外部接続の失敗、AI呼び出しの失敗、データ解析の失敗など）を識別し、アプリが織り込み済みの「想定内の失敗」として分類するために、`AssessmentTerminalError` などの基底クラス（マーカー）を定義しています。

今回の `CategoryEnumDatabaseMismatchError (EnumとDBの不一致)` は、そうした「AIや外部接続に起因する想定内の失敗」の分類からあえて外し、基底クラス（マーカー）を継承させずに素の Exception 直下に置いています。

これにより、AIや外部通信の失敗識別ロジック（match exc:）を意図的にすり抜けさせ、case _:（想定外バグの捕獲枠）へ落とすことで、システム異常としてスタックトレースとともに記録されるようにしました。

```python
match exc:
    case AssessmentTerminalError():      # 想定内・処理を終了する失敗
        ...  # 監査に、その失敗固有の code を焼く
    case AssessmentRecoverableError():   # 想定内・再試行しうる失敗
        ...  # 監査に焼いて、リトライ
    case SQLAlchemyError():              # 想定内・DB エラー
        ...
    case _:                              # 上のどれにも当てはまらない = 想定外
        await self._audit_unexpected_failure(ready, exc)  # unexpected_error として焼く
        logger.exception(...)            # スタックトレースも残す
```

これで、監査ログの上でも「想定内の失敗」と「システムバグ」がはっきりと区別されるようになりました。

しかし、このバグは実行時に起きてから気づくのでは遅すぎます。DBマスタの不整合はデプロイ時に検知すべき問題です。
そこで対処の主軸として、Worker 起動時（WORKER_STARTUP）に Enum が DB マスタに揃っているかを検証し、欠けていれば即座に起動を止める（fail-fast）仕組みを導入しました。

```python
async def assert_category_catalog_covers_enum(self) -> None:
    db_slugs = await self._load_db_category_slugs()
    missing = missing_category_slugs(db_slugs)  # enum のうち DB に無い slug
    if missing:
        raise CategoryEnumDatabaseMismatchError(missing)
```

## 7.5 第7幕の終わりに

今回の見直しで自分にとって一番大きかったのは、「失敗を検知した場所が、その『なぜ（理由）』まで伝えること——そこまでがその処理の責任」 という考えを持てたことです。

失敗を正しく定義するには、その処理が何をしているのかを根本から考え直す必要がありました。「実装前に理解してはじめて、適切な失敗を定義できる」という重要性を再認識しました。

正直に言えば、蓄積され始めたこの記録を、まだプロダクトの改善に活かしきれてはいません。しかし、「どの取得元の記事がどこで失敗したのか」「AIの応答がなぜ弾かれたのか」が、明確な理由とともに残る基盤は整いました。

このデータを積み重ねていけば、記事を安定して供給できているソースの特定や、AI処理で頻発する失敗パターンの把握が可能になるのではないかと考えています。

次は取得元の見直しや、プロンプト・スキーマの改善など、この記録を実際の改善へつなげていきたいと考えています。


## 全体を振り返って

このアプリケーションを作り始めた頃は、もっと無邪気に「より良いものになる」と漠然と思い描いていました。
けれど現実には、著作権による収集データの制限、AI 利用料やデプロイにかかるコストなど、さまざまな制約が存在します。
「何のために、どのようなアプリケーションを作るのか」というプロダクトとしての視点を最初から持つ重要性を、作りながら身をもって学んできました。

進めば進むほど、自分の「知らないこと」の多さに圧倒される感覚があります。
こうして振り返りを書いている最中も、まだ十分に設計しきれていない部分や、初期に AI に依存して設計していた頃の曖昧な命名が目につき、課題の多さを痛感しています。
だからこそ、最初から自分の意志で設計しなければ、後から取り戻すことは難しいのだと深く学びました。

しかし同時に、ドキュメントとして文字にし、思考をアウトプットするプロセスそのものが知識の定着につながり、コードの違和感に気づくための強力なツールであることにも気づけました。この「書いて振り返る」取り組みは、今後も絶対に続けていこうと思います。

とても長くなりましたが、最後まで読んでいただき本当にありがとうございました。これからも妥協せず、学習と実践を続けていきたいと思います。
