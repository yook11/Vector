[← 目次](README.md) ・ 前: [第4幕](04-investing-in-value.md)

# 第5幕 — 監査ログをきっかけに

第4幕で価値の中心に手を入れ始めると、次に浮かび上がったのは、パイプラインで何が起きたのかを後から追えないという問題でした。
記事が分析まで到達していないとき、取得に失敗したのか、本文の補完で止まったのか、それともAIの呼び出しで失敗したのかを判別できませんでした。

第5幕は、この問題を解消するために、監査ログである pipeline_events を導入したところから始まります。それをきっかけに、失敗の性質そのものと向き合うことになりました。
それまでも、エラーハンドリングはある程度できているつもりでした。ところが、監査ログを導入する過程で、それだけでは不十分だったことが次々と明らかになっていきます。

監査ログを素直に組み込めない場所には、決まって複数の問題が混ざっていました。どこで記録するのか。失敗をどう分類し、どう扱うのか。そして、何を記録すべきなのか。

振り返れば、この章で扱う監査基盤は、AIの提案をただ受け入れるのではなく、自分で問いを立てながら設計に向き合った、初めての経験だったように思います。


## 5.1 意図のないテーブル

監査の必要性は、5月に突然生まれたものではありません。4月にはすでに、記事取得の成否や件数、エラー内容、処理時間をDBに残す fetch_logs がありました。

ただ、振り返れば、これはかなり場当たり的な設計でした。
後から何を確認したいのか、そのために何を記録すべきかを定めないまま、「とりあえず実行結果を残す」ために作っていたからです。

そのため、処理が失敗したことは分かっても、ネットワーク接続、レスポンスの解析、記事の保存のどこで問題が起きたのかまでは追えませんでした。
失敗の原因を後から調べられる設計にはなっていなかったのです。

| column | meaning |
|---|---|
| `id` | ログID |
| `source_id` | 取得対象のニュースソース |
| `status` | `success` / `error` |
| `articles_count` | 取得できた記事数 |
| `error_message` | エラー内容 |
| `duration_ms` | 処理時間 |
| `fetched_at` | 取得処理を実行した時刻 |

もう一つの問題は、fetch_logs を処理の記録だけでなく、次回の取得範囲を決めるためにも使っていたことです。
前回の取得成功時刻を fetch_logs から調べ、それを基準に次の取得範囲を決めていました。

つまり、過去の出来事を残す監査の責務と、次の処理を決める状態管理の責務が、一つのテーブルに混ざっていたのです。

`fetch_logs` を拡張する案もありましたが、責任が曖昧なまま継ぎ足すだけになると判断し、設計し直すことにしました。

必要だったのは、失敗したときに、どの工程で何が起き、なぜ処理が止まったのかを後から追えることでした。特に非同期ワーカーの失敗は、途中で処理が止まっても、そのまま見えなくなってしまいます。

そこで掲げた目標は、未来の自分が「何が起きていたのか」を追跡できること。
そのために、記事の取得、情報の補完、AI分析、埋め込みまで、パイプライン全体の出来事を記録する pipeline_events の設計を始めました。


## 5.2 監査を広げるほど、問題点が見えてくる

`pipeline_events` は、最初から完成した監査基盤として導入したわけではありません。記事の取得、本文の補完、AI分析と、工程ごとに少しずつ監査を広げていきました。

しかし、実際に監査ログを書こうとすると、既存のエラー分類だけでは十分でないことが見えてきました。

外部ページから本文を補完する処理では、すでに取得失敗を二つに分けていました。

```python
class PermanentFetchError(Exception):
    """リトライ不可のフェッチ失敗（403 / 404 / robots.txt で拒否）。"""

class TemporaryFetchError(Exception):
    """リトライ可能なフェッチ失敗（5xx / タイムアウト / 429）。"""
```

この分類から分かるのは、失敗がリトライ可能かどうかだけです。

リトライ可能な失敗の中には、5xx、429、タイムアウトが含まれます。
原因の異なる失敗が、同じ分類にまとめられていたのです。

そのため、分類名をそのまま監査ログに記録しても、具体的に何が原因で失敗したのかまでは判別できません。

```python
except PermanentFetchError as e:
    await self._audit_terminal(
        ...,
        # 404 なのか、403 なのか、robots.txt 拒否なのかは
        # reason_code だけでは区別できない
        reason_code="permanent_fetch_error",
        exc=e,
    )
    return TerminallyDropped(reason_code="permanent_fetch_error")
```

リトライ可能な失敗の扱いを考え直し、`TemporaryFetchError` を回復の性質ごとに分けました。

```python
class TemporaryFetchError(Exception):
    """リトライ可能だが、性質を分類できていない失敗。"""


class ServerErrorBlip(TemporaryFetchError):
    """短時間で回復する可能性がある通信障害。"""


class ServerErrorOutage(TemporaryFetchError):
    """長引く可能性があるサーバー障害。"""


class ServerErrorRetryAfter(TemporaryFetchError):
    """サーバーから再試行までの待ち時間を指定された失敗。"""


class ReadTimeout(TemporaryFetchError):
    """レスポンスの読み取りがタイムアウトした失敗。"""
```

さらに、再試行の間隔と上限を `RetryPolicy` として表しました。

```python
@dataclass(frozen=True)
class RetryPolicy:
    code: str
    max_attempts: int
    delay_minutes_schedule: tuple[float, ...]
```

短時間の障害なら短い間隔で試し、長引く障害なら間隔を広げる。サーバーから `Retry-After` が返された場合は、その指示を優先する。
このように、失敗の性質に応じて再試行方法を変えられるようになりました。

監査ログにも、再試行することや、上限に達して処理を終了したことを残しました。

```python
reason_code = f"temporary_will_retry_{policy.code}"
```

```python
reason_code = f"temporary_exhausted_{policy.code}"
```

これによって、失敗を以前より細かく捉えられるようになったと思っていました。
確かに、失敗を分類するという意識は生まれていましたが、この時点で記録していたのは、主に「どのように再試行したのか」という処理上の判断でした。

本来、監査ログから知りたかったのは、「そこで何が起き、なぜ失敗したのか」という事実です。ところが、当時の設計はその目的から少しずれていました。
さらに、再試行しない失敗については、その原因を分類することさえできていませんでした。

失敗の分類を見直し始めてはいたものの、監査に残すべきなのは「その失敗をどう扱ったか」だけではないことを、この時点ではまだ十分に捉えられていなかったのです。


### AI 分析側の失敗分類

AI 分析側では、AI の呼び出しで発生した失敗を、`ConfigurationError` や `ProviderError`、`NetworkError` といった大まかな例外へ変換していました。

```python
class ConfigurationError(AnalysisDomainError): ...
# API key やモデル名など、設定・認証の問題

class ProviderError(AnalysisDomainError): ...
# provider 側の障害や、期待した応答が返らない問題

class NetworkError(AnalysisDomainError): ...
# タイムアウトや接続失敗などの通信問題

class RateLimitError(AnalysisDomainError): ...
# レートリミット制限

class InsufficientBalanceError(AnalysisDomainError): ...
# 残高不足による停止
```

そこで最初は、この既存の分類をそのまま監査ログに使おうとしました。
task 側で例外を捕捉し、例外型ごとに `outcome_code` を記録するとともに、処理を再試行するか、そのまま終了するかも同じ分岐の中で判断していました。

このコードでは、`ConfigurationError` と `InsufficientBalanceError` は、監査ログを残して処理を終了します。
一方、`NetworkError` は、リトライの上限回数まで再試行します。

```python
except ConfigurationError as exc:
    await _audit_extraction_failure(
        ...,
        outcome_code="ai_error_config",
        ...
    )
    return

except InsufficientBalanceError as exc:
    await _audit_extraction_failure(
        ...,
        outcome_code="ai_error_insufficient_balance",
        ...
    )
    return

except NetworkError as exc:
    if is_last_attempt(ctx):
        await _audit_extraction_failure(
            ...,
            outcome_code="ai_error_network",
            ...
        )
        return
    raise
```

それまで、エラーハンドリングはある程度できているつもりでした。
しかし、既存の例外分類に沿って監査ログを入れてみると、どこか不自然に感じました。その違和感を整理していくと、二つの問題が見えてきました。

一つは、AI 処理のエラー分類が粗すぎることです。`ProviderError` のような大きな括りだけでは、AI プロバイダーの一時的な障害なのか、応答形式が壊れていたのか、安全上の理由で拒否されたのかを区別できません。処理として失敗を扱うことはできても、後から「何が起きたのか」を追うには不十分でした。

もう一つは、失敗後の扱いが task 内のコードを読まなければわからなかったことです。
例外型から分かるのは、大まかな失敗の原因までです。それを再試行するのか、そのまま終了するのかは、`raise` や `return` を含む task 側の分岐を読まなければいけません。

さらに、監査ログへ記録する `outcome_code` も、それぞれの分岐に直接書かれていました。

一つの失敗について、「何が起きたのか」「そのとき処理をどうするのか」「監査ログに何を記録するのか」が、ひとまとまりとして定義されていませんでした。

たとえば `NetworkError` が通信の失敗を表すこと、最終試行までは再試行すること、監査ログには `ai_error_network` と記録することが、それぞれ個別に決められていました。
そのため、どれか一つを変更したときにほかが取り残され、実際の処理と監査記録に食い違いが生まれる可能性がありました。

そこで、失敗の原因と、その後の扱い、監査ログへ記録する名前を、例外型の側でまとめて表せないかと考えました。

AI の呼び出しで起こりうる失敗を、原因ごとの例外型として定義し直し、それぞれに `CODE` と marker を持たせました。
`CODE` は、監査ログに記録する内容。marker は、再試行するのか、再試行せず終了するのかといった処理方針を表す目印です。

さらに、失敗後の処理方針は型の継承によって表しました。
`RetryableError` を継承する例外は再試行の対象とし、`NonRetryableKeepArticle` を継承する例外は、再試行せず記事を残して終了するものとして扱いました。

```python
class AIProviderNetworkError(AIProviderError, RetryableError):
    CODE = "ai_error_network"
    INLINE_RETRY = True

class AIProviderInsufficientBalanceError(AIProviderError, NonRetryableKeepArticle):
    CODE = "ai_error_insufficient_balance"
```

この形にすれば、失敗の種類と、その失敗をどう扱うかを、一つの例外型にまとめられると考えました。

ところが設計を進めるうちに、同じ AI プロバイダーの失敗であっても、どの工程で起きたかによって扱いが変わることに気づきました。

そこで必要になったのは、AI プロバイダーに共通するエラー分類はそのまま使いながら、その扱いだけを工程ごとに定義する方法でした。
共通のエラーと工程固有の処理方針をどのように組み合わせるかを調べた結果、当時は、同じ扱いをする例外型をタプルにまとめる方法が最もシンプルに実装できると考えました。

```python
ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS: tuple[
    type[AIProviderError], ...
] = (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderQuotaExhaustedError,
)

ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS: tuple[
    type[AIProviderError], ...
] = (
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)


def map_provider_to_assessment(
    exc: AIProviderError,
) -> AssessmentError:
    """共通の AI provider error を、Assessment 工程での扱いを表す例外へ変換する。"""

    if isinstance(
        exc,
        ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS,
    ):
        return AssessmentRecoverableError(
            str(exc),
            code=exc.CODE,
            provider_error=exc,
        )

    # 再試行しないエラーは、AssessmentTerminalSkipError に変換する
    ...
```

task 側では、個別のプロバイダーエラーではなく、工程ごとの扱いを表す変換後の例外を捕捉するようにしました。

```python
try:
    result = await svc.execute(ready, classifier)

except AssessmentTerminalSkipError as exc:
    await record_assessment_failure(
        session_factory,
        ready=ready,
        exc=exc,
        attempt=attempt,
    )
    return

except AssessmentRecoverableError as exc:
    await record_assessment_failure(
        session_factory,
        ready=ready,
        exc=exc,
        attempt=attempt,
    )

    if is_last_attempt(ctx):
        return

    raise
```

これによって、個々のプロバイダーエラーをどう扱うかという定義を、task 内の条件分岐へ分散させず、工程ごとのタプルに集約できました。
同時に、AI プロバイダーに共通するエラー分類は再利用しながら、工程ごとの違いは、どのタプルに含めるかによって表現できるようになりました。
task は変換後の例外だけを見て、再試行する失敗と、そのまま終了する失敗に共通する処理を、それぞれ一つの分岐で実行できるようになったのです。

この設計によって、失敗の扱いは整理できました。しかし、「なぜ失敗したのか」を後から理解するための情報は、まだ十分ではありませんでした。

当時、監査ログに記録していたのは、例外型に定義した `CODE` だけでした。そこから分かるのは、システムがその失敗をどの種類のエラーとして分類したかまでです。

たとえば、`ai_error_network` からは通信に関する失敗だと分かりますが、タイムアウトなのか、接続に失敗したのかまでは判断できません。
`ai_error_input_rejected` も、AI プロバイダーに入力を拒否されたことしか分かりません。
入力が長すぎたのか、安全性ポリシーに触れたのかによって、入力を短くしてやり直すべきか、再試行を諦めるべきかは変わります。
しかし、`CODE` だけでは、その判断に必要な原因までは残せていませんでした。

この不足が、後に失敗の型や `reason_code` を見直すきっかけになりました。


## 5.3 何でも記録すればいいわけではない

監査ログを導入し始めた頃は、後からその時点で何が起きていたのかを、できる限り正確に復元できるようにしたいと考えていました。
そのためには、関係する情報をなるべく多く記録するべきだと思っていたのです。

たとえば、AI 分析の結果を DB に保存したときに発行される、分析結果レコードの ID（`assessment_id`）や、AI が返したカテゴリに対応する、DB のカテゴリマスタの ID（`category_id`）まで、監査ログに残そうとしていました。
カテゴリの構成が後から変わっても、その記事を当時どのカテゴリとして扱ったのかを追えるようにしたい、という意図がありました。

ところがコードを読み返すと、監査ログへ記録する情報を揃えるためだけに、関連するテーブルを JOIN して値を取得している箇所があることに気づきました。本来の処理では必要のない問い合わせが、監査のために追加されていたのです。

その不自然さを辿っていくと、目的がずれていることに気づきました。

監査に残すべきなのは、後から検索しやすくするために集めた値ではなく、その時点で実際に起きた出来事と、その時点で得られた事実です。工程をまたいで追跡したいのであれば、相関 ID のような仕組みでつなぐべきこともこの時点で学びました。

この気づきは、失敗の記録を見直すきっかけにもなりました。

「その工程で起きたことを、その場で記録する」という視点で見直すと、当時の監査ログから分かるのは、失敗したという結果まででした。そこで具体的に何が起きたのか、十分に残せていなかったのです。


## 5.4 第5幕の終わりに

起きた出来事を、後から読み返して理解できる形で残すには、失敗を単に「失敗」としてまとめるのではなく、何が起きたのかを明確な言葉で表す必要があると実感しました。

失敗の性質を見極めることは簡単ではなく、同じ種類の失敗であっても、どの工程で起きたかによって、扱いが変わることがあります。
失敗の原因だけでなく、それがどこで起き、アプリケーションにとってどのような意味を持つのかまで考えることが、設計において重要なのだと気づきました。

その中で、「その時点で起きたことを、正確かつ自然な形で記録する」という視点を得られたことは、大きな転換でした。
この視点は、その後の設計に違和感を覚えたとき、問題を見つけて修正していくための手がかりになりました。

個人的には、この監査基盤の設計が、AI の提案に頼って進める段階から、自分でコードを理解し、設計を考える段階へ移る転換点だったと思います。

監査ログを導入し始めた頃は、なかなか思うように進まず、AI の提案を頼りに、とにかく動くところまで実装することを優先していました。しかし、後からコードを読み返すと、なぜそのような構造になっているのかを自分でも説明できない、分かりにくいものになっていました。

その違和感をきっかけに、コードで何が起きているのかを、自分で理解しようとするようになりました。
監査によって何を知りたいのか、どのような状態を目指すのかを自分で問い、そのために何をするのかを自分で決める。
そうした姿勢が生まれ始めたのは、この頃だったと思います。

ただし、「失敗とは何か」という問いに、この時点で答えを出せたわけではありません。その後も何度も設計に違和感を覚え、そのたびに見直すことになります。この問いには、第7幕で再び向き合います。

次の第6幕では、アプリケーションと向き合っていきます。

次: [第6幕 — アプリケーションの概念と向き合う](06-domain-model-rebuild.md)
