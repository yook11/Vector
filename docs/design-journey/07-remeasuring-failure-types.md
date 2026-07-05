[← 目次](README.md) ・ 前: [第6幕](06-domain-model-rebuild.md)

# 第7幕 — 失敗と向き合う

工程の概念を整理し直していく中で、その工程で捉えなければならない失敗も見えてきました。

監査ログを一通り入れ終わったあと、残された内容を見返すと、まだ十分に表現できていないものがあることに気づきます。

どの工程で失敗したのかは分かる。けれど、その工程で何が起きたのかまでは分からない。

ここからは、工程ごとの失敗をもう一度見直し、何が起きたのかを後から読み返せる形にしていきます。


## 7.1 理由は、検知した場所が持つ

「何が起きたのか」が最初に曖昧だったのは、パイプラインの入口,外部ソースを読む Reader でした。

Reader の仕事は、外部ソースから返ってきた response を、アプリケーションが扱える軽い Entry に写すことです。

```python
@dataclass(frozen=True)
class HackerNewsEntry:
    url: str | None
    title: str | None
    published: datetime | None
    raw_created_at: str | None
```

Reader は、外部形式ごとの違いをまずここで受け止めます。

この部分で考えられる失敗は、外部ソースから帰ってきたレスポンスが空、JSON や XML として壊れている、こちらが期待している応答形式と合っていないということが起きます。

けれど当時は、こうした違いがすべて `read_unreadable_response` という一語に潰れていました。Reader が読めなかったことは分かっても、何を読めなかったのかまでは分からなかったのです。

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

取得したデータをアプリケーション上の概念へ変換していく段階でも、
同じように失敗理由を構造化する必要がありました。
中でも重要なのは、記事を分析可能な AnalyzableArticle へ昇格させる段階です。

条件を満たせなかったとき、以前は構築に失敗した理由を、Pydantic(バリデーター) の検証メッセージをそのまま使用していました。しかし、入力の値が、そのまま監査ログに永続化される問題や、後からどの部分が原因で構築が失敗したのか？を追うことができないものになっていました。

そこで、昇格できなかった理由を、「どの不変条件に届かなかったか」定義しました。

```python
class AnalyzableArticleDefect(StrEnum):
    TITLE_MISSING = "analyzable_article_title_missing"  # タイトルがない
    BODY_MISSING = "analyzable_article_body_missing"  # 本文がない
    BODY_TOO_SHORT = "analyzable_article_body_too_short"  # 本文が短すぎる
    PUBLISHED_AT_MISSING = "analyzable_article_published_at_missing"  # 公開日時がない
```

そして、これらを束ねて、「品質が足りず昇格できなかった」ことを `QualityTooLow` として表しました。

```python
@dataclass(frozen=True)
class QualityTooLow:
    defects: tuple[AnalyzableArticleDefect, ...]
```

構築を試みて品質に届かなかった時、これでバリデーターのメッセージがそのまま使用されることは無くなりました。

```python
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

AI の応答は、必ずしもこちらの期待したスキーマを守るわけではありません。key が欠けることも、値の型が違うこともあります。なぜそうなったのかを記録できるかどうかは、そのままプロンプトやスキーマの改善につながります。

けれど当時は、その違いが `outcome_code = "assessment_response_invalid"` につぶれていました。

そこで、collection でしたのと同じように、失敗を検知した場所——応答を parse する場所——が、何が起きたのかを defect として持つようにしました。

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

他の工程では失敗を例外として投げ、その定義に「なぜ失敗したのか」を `code` や `reason` として持たせていました。

一方で、外部から取得した情報をアプリケーションの概念へ変換する段階では、最初から分析に進める品質を満たせないことを想定内の出来事として扱っていました。

他の工程とは異なり、例外ではなく通常の分岐で表していたため「失敗に理由を持たせる」という発想が抜け落ちていました。

```python
def convert_fetched_article(
    fetched: FetchedArticle,
) -> AnalyzableArticle | ObservedArticle | None:
    if not fetched.title:
        return None   # title が無ければ、変換できないことをNoneで表す。
    ...
```

`None` は「変換できない」を一括りにしていましたが、そこには意味の違う失敗が混ざっていました。

分析に進める品質に届かないだけなら、それは失敗ではありません。取れた事実を `ObservedArticle` として残し、本文や公開日時は、後続の補完工程が URL を辿って埋めます。例外ではなく、補完待ちへ進むための通常分岐です。

けれど、title と URL が無いと、その `ObservedArticle` すら組み立てられません。この二つは記事の identity だからです。URL は記事の住所であり、重複排除のキー（`incomplete_articles.url` の UNIQUE 制約）でもあります。取得の起点そのもので、他から補うことができません。title は「これが記事である」と言える最小の中身です。

だから、品質不足は「補完待ち」で済みますが、identity の欠落は「記事として成り立たない」棄却になります。`None` で静かに落としていたこの失敗は、後から追える理由を持つべきでした。

そこで、変換できなかったときに、なぜか？という理由を伝えるようにしました。

```python
class AcquisitionConversionDefect(StrEnum):
    TITLE_MISSING = "acquisition_conversion_title_missing"
    # title がなく、ObservedArticle としても残せない

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

呼び出し側で、想定外のバグだけを例外として受ける.

```python
try:
    outcome = convert_fetched_article(fetched)
except Exception as exc:
    # 例外として受け、UNEXPECTED_ERROR + stack trace で残す。
    outcome = unexpected_rejection(fetched, cause=exc)
```


## 7.4 想定内のエラーにも種類がある

失敗を「想定内」として一つずつ定義していく中で、その中に起きるかもしれないバグがあることに気づきました。

投資判定の工程では、AI が選んだカテゴリを、DB に登録されたカテゴリへ解決します。この解決に失敗したとき、当初は「AI が catalog に無いカテゴリを返した」—— 想定内の失敗として扱っていました。

```python
class AssessmentCategoryMissingError(AssessmentTerminalError):
    """AI が catalog に存在しない category slug を返した。"""
    ...
```

けれど、この「解決できない」という同じ症状の中に、原因も直し方もまったく違うものが混じっていました。

アプリ側の enum にもカテゴリを定義していて、それをAIに渡し、その中から選ばせる処理になっています。
アプリケーション側に新しいカテゴリを足したのに、DB への登録（seed）を忘れると、その値は「アプリでは有効なのに、DB には無い」状態になります。この時原因は AI ではなく アプリケーション側のバグということになります。

そこで、このずれを専用の型で表し、想定内の失敗から切り離しました。

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

想定内のエラーが属する marker 分岐には当たらず、想定外—に落ちて 記録されます。想定内の失敗とは、監査の上でもはっきり別のものになります。

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

さらに、このバグは、起きてから気づくのでは遅すぎます。そこで worker の起動時に、enum が DB に揃っているかを検証し、欠けていれば起動を止めるようにしました。

```python
async def assert_category_catalog_covers_enum(self) -> None:
    db_slugs = await self._load_db_category_slugs()
    missing = missing_category_slugs(db_slugs)  # enum のうち DB に無い slug
    if missing:
        raise CategoryEnumDatabaseMismatchError(missing)
```


## 7.5 第7幕の終わりに

振り返ると、この見直しで自分にとって一番大きかったのは、失敗を検知した場所が、その「なぜ」まで伝える——そこまでが責任なのだ、という考えを持てたことでした。

失敗を定義するためには、その工程が何をしようとしているのかを、もう一度考え直すことになりました。
初めからどのような失敗があるのか？考えることも重要なのではないか？ともうようになりました。

正直に言えば、この記録を、まだ改善には活かしきれていません。
けれど、記録には、どのソースから取れた記事がどこで失敗したのか、AI の応答がなぜ弾かれたのかが、理由とともに残るようになりました。これを積み重ねていけば、どのソースが分析に進める記事を安定して出しているのか、AI 処理のどの失敗が多いのかが見えてきます。取得元の見直しや、プロンプト・スキーマの改善に、つなげていける素地ができたということです。

なぜ失敗したのかを残すことは、改善そのものではなく、改善を始められる場所をつくることでした。