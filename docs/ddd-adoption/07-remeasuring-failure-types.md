[← 目次](README.md) ・ 前: [第6幕](06-domain-model-rebuild.md)

# 第7幕 — 失敗の型を、測り直す

第6幕で取得パイプラインを作り替えるうちに、「失敗を表す型」がいくつも増えました。変換の棄却、観測どまり、品質不足——成功と失敗を別の型に分けるほど、構造は安全に見えます。当時の私は、そう信じていました。失敗をきちんと型で表し、種類ごとに分けるほど、丁寧で安全な設計になる、と。第7幕(2026年5月下旬〜6月上旬)は、その**「型を増やすほど安全」という、私自身の信念のほうを測り直していった**話です。

そもそもの出発点では、失敗は「型」ですらありませんでした。3月のスキーマ設計原則には、`consecutive_errors` や `last_error_message` を「一時的なエラー状態」としてワーカーやログへ追い出し、本文取得の再試行カウンタ(`content_fetch_attempts`)は Redis キューで持つ、と書いています(`design-principles.md` §5「パイプライン状態は DB の外へ」, 2026-03-22)。失敗は、DB のカラムに書き込む状態か、キューに積む再試行回数でした。そこから「失敗を型で表す」へ動き、さらに「では型をどれだけ作るのか」を問い直す——この幕には、二段の測り直しがあります。

なお、この時期も語彙の統一や改名が並行していたため、当時の名前と現在の名前がずれる箇所があります。逐語を引くときは、その都度ことわります。

## 7.1 失敗に、理由を語らせる

最初に変えたのは、失敗を「例外を投げて、監査には `str(exc)` を焼けばいい」と扱っていたところでした。

きっかけは、第5幕で入れた監査(探針)が、自分の作ったものを照らし返してきたことです。監査を読み返すと、`outcome_code`——その段で何が起きたかを表す主契約のコード——が、粗いタグ一本に潰れていました。当時の横断的な監査メモは、この症状をこう書いています。「ドメインが原因を構造化して持っているのに、`outcome_code` に焼かず free-text に逃がす」経路が各段に点在している、と(`audit_chaeck.md`、同時代メモ)。失敗の理由は `payload` の `error_message`(`str(exc)` を redact したもの)に押し込まれ、`outcome_code` 単独では「何が起きたか」を集計できない状態でした。

そこで、失敗の理由を、発生源のドメイン(値オブジェクトや集約)に**型で語らせる**ようにしました。まず構築失敗の証拠化を `AnalyzableArticle.build_or_reject` に移し(#595, `577835d7`, 5/23)、完成段の失敗型を `CompletionRejection` 一段に縮約しています(#599, `64b3ff23`, 5/23)。この時点ではまだ理由は free-text(`error_class` / `error_message`)でした。次に、VO 構築失敗の理由を VO 固有の reason enum として発生源から型で表し(#675, `fa426701`, 5/29)、続けて、それまで全件 `completion_invariant_rejected` に潰れていた不変条件の棄却を、「本文が足りない」「公開日時が無い」「タイトルが長すぎる」のように `AnalyzableArticleDefect`(`StrEnum`)で構造的に区別しました(#680, `42be8ca6`, 5/29)。

このとき、取得側に非対称が残っていました。先の監査メモは、acquisition の変換棄却が「`REJECTED` を `article_conversion_rejected` 一本にして、`ConversionReason` を捨てている。completion の `AnalyzableArticleDefect` と非対称」だと名指ししています。これを、責任元の VO が持つ reason を verbatim(そのまま)で `outcome_code` に運ぶ値化へ揃え、対称にしました(#682, `b97787e6`, 5/30)。

いまのコードでは、これらの失敗理由は `StrEnum` の値がそのまま監査の `outcome_code` になります——`AnalyzableArticleDefect`(9 値, [analyzable_article.py](../../backend/app/collection/domain/analyzable_article.py))、`AcquisitionConversionDefect`(2 値, [errors.py](../../backend/app/collection/article_acquisition/errors.py))、`SafeUrlInvalidReason`(5 値, [safe_url.py](../../backend/app/shared/security/safe_url.py))。第5幕では監査が「どこで何が起きているか分からない」と告げ、第6幕では失敗を値にしました。第7幕の最初の一歩は、その値に、機械が読める理由を型として持たせたことでした。

## 7.2 値だけで、原因が読める

理由を型に持たせると、すぐ次の問いが来ます。**その型に、何を載せ、何を載せないか**です。

最初に決めた規律は、「値=コード」でした。defect / reason の `StrEnum` の値そのものを `pipeline_events.outcome_code` に焼く。理由のラベルと監査のコードを別々に持つと、また二重表現になるからです。この統一は最後に横断的に揃えています(`outcome_code` の `StrEnum` 統一、#786, `e6ec9076`, 6/11)。

二つめは、PII を型ガードで構造的に締め出すことでした。当時の VO の設計メモにも「エラーメッセージに入力値を含めない(ログへの PII / 悪意ある URL 混入防止)」とあります(`structural-guarantees.md`、同時代メモ)。これを失敗型でも徹底し、AI が生成した値・本文の断片・生の URL を、理由の型のコンストラクタに通さないようにしました。理由は `StrEnum` のメンバーでなければ受け取らず、`AnalyzableArticleDefect` の品質判定では、Pydantic が返す英語メッセージ(本文断片を含みうる)は保持せず、構造化した defect だけを焼きます。失敗の監査に「どの値で失敗したか」ではなく「どの種類の失敗か」だけを残す、という線引きです。

三つめは、**失敗を検知した場所が、その語彙を所有する**ことでした。AI 応答の schema 違反は、それを検知する parse がその語彙を持ち、各メンバーが raise 点と 1 対 1 に対応するので写像漏れが原理的に起きません(`AssessmentResponseDefect`、10 値, [parse.py](../../backend/app/analysis/assessment/ai/parse.py))。一方、provider が機構契約(tool call の有無や JSON の体裁)を破った状態は、parse が扱う「内容の schema 違反」とは別レイヤなので、検知する adapter 側が provider ごとに持ちます(`DeepSeekResponseDefect` / `GeminiResponseDefect`)。検知場所所有の defect で監査を具体化したのは #735(`52cd61c5`, 6/5)です。

そして、粒度を二段に分けました。粗い集計軸(`outcome_code`)に複数の状態を畳み、細かい forensic 軸(`failure_reason`)でそれらを区別する。たとえば AI provider の状態理由は、`GeminiStateReason`(14 値)や `DeepSeekStateReason`(11 値)が同一の `outcome_code` に畳まれる複数状態を区別します。集計に使う軸と、原因調査に使う軸を、別の粒度として持たせた格好です。

この区別の最上位にあるのが、`EventType` の四値です——`SUCCEEDED` / `SKIPPED` / `REJECTED` / `FAILED`([event.py](../../backend/app/audit/domain/event.py))。`REJECTED` は「ドメインが reason code で説明できた停止」、`FAILED` は「想定外」。当時の precondition 設計メモにも、同じ線引きが tracked で残っています——「楽観的ロック敗北は想定内・業務正常、データ整合性違反は想定外・バグ indicator。前者は戻り値、後者は例外」(`typed-pipeline-preconditions.md`)。この節は、第5幕で監査を「ありのままの事実だけを記録する witness」に純化したことの、失敗型の側からの続きでした。

## 7.3 増やした型を、畳む

ここまでは「型を増やす」方向の話です。最初に逆向きの——増やした型を畳む——測り直しが起きたのは、取得まわりの marker でした。

「型を増やすほど安全」を信じていた時期、取得失敗を marker 例外の階層へ寄せ(#643, `19f52380`, 5/27)、読取失敗も単一の例外から `UnreadableResponseReason` 駆動の自己記述へ実体化しました(#687, `2f8f3510`, 5/30)。種類ごとに型を分けるほど丁寧だ、という方針です。その結果、marker が三つ(取得 marker・外部 fetch・読取不能)に増えました。

ところが、その三つの区別は、発生源の例外が CODE と型で既に語っていました。marker 側は、それを複製しているだけでした。そこで、過分解されていた三つの marker を `AcquisitionReadError` 一本へ集約しています(#689, `2d5fb22e`, 5/30)。畳んだ根拠は、「fetch / read の区別は origin が CODE + 型で既に自己記述しているため、marker 側の複製を畳む」こと。あわせて、もう到達しない `isinstance` ガードも除いています。同時代のメモには、これと同型の発見がもっと率直に書かれています——「Stage 4 設計時に既存パターンを照合し、自分の spec が不要な Stage 4 専用派生を量産していたことを発見して畳んだ」(`pipeline-failures.md`)。これが、「型を増やすほど安全」という自分の信念に最初に向けた測り直しでした。

面白いことに、増やすときの判断にも、すでに「増やさない」が混じっていました。読取失敗を実体化した #687 では、「parse / structure は説明上のグループにすぎず、型でも属性でもない」として型分割せず、format(xml / json / feed)は CODE に焼かず安全文脈に持たせ、`retryable` 属性も持たせていません。いまのコードでも、`AcquisitionReadError` は fetch / read の origin を hold し([errors.py](../../backend/app/collection/article_acquisition/errors.py))、`UnreadableResponseReason` は 4 値で、format 軸を CODE に焼いていません([read_errors.py](../../backend/app/collection/article_acquisition/reader/read_errors.py))。

畳む対象は、使われなくなった型そのものにも及びました。先の監査メモが「現役は `external_fetch_errors.py`」と名指していた、importer ゼロの `SourceFetchError` 例外階層を削除しています(#743, `53d851d8`, 6/5)。

## 7.4 分岐するのは、処理方針

次の測り直しは、もっと根の深いものでした。失敗の型は、そもそも**何の軸で分けるべきか**、という問いです。

最初の分類は二値でした。永続的失敗か、一時的失敗か——`Permanent` / `Temporary` です(`collection-ingestion-errors.md` には「304 は retry 軸(Permanent / Temporary)のどちらでもない」と tracked で残っています)。けれど、ここでは原因(何が起きたか)と処理方針(どう扱うか)が混ざっていました。当時のメモで、自分はこう整理しています——「ビジネスロジックが分岐すべきは処理方針(retryable / drop / keep / unknown)であり、原因(provider 系か stage 固有か)は監査・調査のための従属軸」。さらに、「同じ原因でも stage によって処理方針が変わる。policy block は Stage 3(本文そのものが拒否)では記事 drop が自然だが、Stage 4(抽出済み要約の分類で拒否)では keep が自然」とも書いていて、原因から処理への固定写像を拒否しています(`pipeline-failures.md`)。

retryability そのものの所在も、測り直しました。briefing の監査設計メモには、「retry-status 駆動(中間 attempt は retryable / 最終 attempt は non_retryable)は撤回」とあります。理由は、intrinsic に retry で直らない例外——たとえば API キー欠落——が、attempt 1/3・2/3 では `retryable` と焼かれ、consumer のアラート(`non_retryable` で拾う)が最大 N-1 attempt 分だけ遅れてしまうから(`pipeline-events-briefing-audit.md` D8、同時代メモ)。retryable かどうかは、何回目の試行かではなく、**例外の型が持つ性質**だ、という測り直しでした。

これらを、分析側で構造として作り替えたのが、6/5 の三段の作業です。AI provider の error に回復クラス(mode)と reason を自己記述させ(#739, `5d967f30`)、`failure_kind` が `terminal_stage_blocked` のような**処理方針(disposition)の語彙を型名に背負っていた**問題を、原因軸(mode)へ移して解消し(#741, `409ca74d`、Phase B)、同じ形を curation にも広げました(#745, `14a36c8f`、Phase C)。このとき marker を縮約し、重複していた tuple を全廃し、特定例外への `isinstance` 補正を一般則へ畳んでいます。

ただし、ここでも畳まない判断を、同じ問いで分けています。curation には marker を一本だけ残しました——`DROP_ARTICLE` という業務副作用を持つ失敗です。一方 assessment は二系統で済ませています(drop が無いため)。「型を分けるかどうか」を、機構として分けられるかではなく、分けた結果を必要とする処理(consumer)があるかで決めた格好です。いまのコードでは、`AIProviderFailureMode`(5 値、回復クラスの mode)と具体状態の reason が二軸に分かれ([ai_provider_errors.py](../../backend/app/analysis/ai_provider_errors.py))、`Retryability`(3 値)と `FailureAction`(`DROP_ARTICLE` の 1 値だけ)が監査の投影軸として独立し([failure_projection.py](../../backend/app/audit/failure_projection.py))、外部取得エラーの family は `retryable: bool`(段に依らない失敗の性質、origin の SSoT)だけを持ち、段の解釈である `Retryability` を知りません([external_fetch_errors.py](../../backend/app/collection/external_fetch_errors.py)。SSoT 化は #684, `0db66afa`)。当時の taxonomy メモに書いた判断基準——「次のアクションが違うなら型を分ける」(`pipeline-events-error-taxonomy.md`、当時の到達点)——が、ここで構造になりました。

## 7.5 安全網が、故障を隠していた

失敗の型を整えていくと、逆の発見もありました。安全に見えていた型が、実は故障を隠していたのです。

一つは、`AssessmentCategoryMissingError` という「想定内の terminal」として置いていた marker でした。けれど、これは AI が返したカテゴリが DB の enum と食い違う——enum と DB の drift というバグです。バグを「想定内 terminal」として監査に並べるのは、実態に合っていませんでした。そこで marker を廃止し、起動時に fail-fast で検出し、それ以外の経路は「想定外」として扱うように変えています(#736, `a985b2fe`, 6/5)。7.2 で引いた「想定外・バグは例外で表現する」を、ここで適用した格好です。

もう一つは、scrape の retry 分類でした。失敗から retry の方針を引く逆引き表を、内包表記で再反転して使う構造をやめ、単一の `match` に畳んだところ(#758, `cfe6aaa3`, 6/7)、その表が 429 の `Retry-After` を黙って捨てていた、という実バグが顕在化しました。型や表を畳んだ瞬間に、隠れていた故障が出てきたわけです。あわせて、前日に入れたばかりの `.decision` プロパティ(条件分岐の無い定数射影)と `ScrapeDecision` を撤去しています(#759, `cd99c6cc`, 6/7。`.decision` を入れた #751 は前日の 6/6)。

測り直しの跡は、現行コードの中にも残っています。DB エラーの分類器([db_errors.py](../../backend/app/audit/db_errors.py))の説明には、元は `getattr(exc, "code", None)` を信用していて、SQLAlchemy がドキュメント参照用に振るコード(`IntegrityError.code="gkpj"` のような値)を拾ってしまっていた、だから明示的な `isinstance` で分類し直した、と書いてあります。これは、第5幕で立てた「故障を隠さない」という規律の、失敗型の側での再演でした。型や表で安全そうに見せていたものが、畳んだ瞬間に隠していた故障を吐き出す——だから、安全網は外から覗ける形にしておく、ということです。

## 7.6 増やさない、という判断

ここまでの測り直しの底に共通していたのは、「型を増やす」と同じくらい、「型を増やさない」を意識的な判断にする、ということでした。

この幕のなかにも、増やさない判断はいくつも出てきます。7.3 の「format は CODE に焼かない / parse・structure は型分割しない / retryable 属性は持たない」(#687)。DeepSeek の応答切れ(truncation)を内容の defect から切り分けたくなったとき、失敗の扱い(retry / 監査 / 例外型)は変えず、構造化ログだけを足して観測できるようにしたこと(#773, `8bf18b7a`, 6/9)。そして 7.4 の、curation に marker を一本だけ残し、それ以外を畳んだこと(#745)。`FailureAction` が `DROP_ARTICLE` の一値だけなのも同じ判断で、「機構として分けられるか」ではなく「分けた結果を要する consumer がいるか」で型を作っています。catch-all についても、当時の監査メモは「設計上の最終手段なので、潰すより stage prefix 付与 + 頻度監視で marker 昇格という運用化が筋」とし(`audit_chaeck.md`)、taxonomy メモは「unknown であることは型では表現できない」と、わざわざ作らない理由を書いています(`pipeline-events-error-taxonomy.md`)。

この「増やさない」は、実はもっと早くから、失敗型に限らず現れていました。4 月のレビューで、グローバル例外ハンドラに `ValueError` を含めない、と決めたとき、自分はこう書いています——「グローバル化の条件は発生源の限定。便利さではなく『バグが隠れないか』で線を引く」(`review-5-sweep.md`)。`ValueError` を一律 422 に写すと、サーバーのバグ(本来 500)がクライアントの入力エラーとして隠れてしまうからでした。同じ頃、domain purity のために分けた二つの型を、より単純な機構(parse 時の VO 検証)が見つかった翌朝に、16 分の三連コミットで自分から畳んでもいます——「正当化が成立しなくなった抽象は維持しない」(`review-4-params.md`)。エラー設計でも「`retry_after` のような属性は 2 つ目のプロバイダが来るまで足さない」と書き(`pipeline-failures.md`)、値オブジェクトについては「1 箇所でしか使わないものに VO はオーバーエンジニアリング。守る価値があるかを毎回問い直している」と残しています(`structural-guarantees.md`)。

つまり、「型を増やすほど安全」という信念は、「分岐するアクションが違うときだけ型を分け、それ以外は増やさない」へと測り直されていきました。当時のメモの言い方を借りれば、「sum type は分岐すべき語彙、例外は対処不能な通知」(`pipeline-failures.md`)。型は、安全のために増やすものではなく、**分岐のために必要な分だけ立てるもの**だ、という線でした。

## 7.7 第7幕の終わりに

いまのコードでは、失敗の語彙が一本で貫通しています。ドメインが分類した defect / reason が、監査への投影([failure_projection.py](../../backend/app/audit/failure_projection.py))を経て `pipeline_events.outcome_code` になり、最後は source health の集計([schemas.py](../../backend/app/admin/source_health/schemas.py))で「どの outcome code が何件出たか」として読めます。これは型をたくさん作った結果ではなく、分岐に必要な軸だけを型に残し、残りを reason や payload へ逃がした結果でした(briefing の race 敗北を Conflict outcome にした #785 や、横断ヘルパーを共通化した #786 も、この収束の一部です)。

考え方の面では、第6幕で設計の問いと目標を自分が握るようになり、この幕ではさらに一歩進んで、**自分自身の信念**——型を増やすほど安全だ——までを測り直しの対象にしました。失敗を型で表すことは正しかったと思います。けれど「だから多いほど良い」は別の話で、そこは「ここに型を投資する価値があるか」で毎回測るべきものでした。

ここでも、一度で正解には着いていません。marker を増やしては畳むまでが同じ週に収まり(#643・#687 が 5/27〜30、畳む #689 が 5/30)、`.decision` を入れた翌日に撤去しています(#751 が 6/6、#759 が 6/7)。増設と撤回の往復そのものが、この幕の実際でした。

型と失敗の次は、それらを支える「道具」の側です。第6幕・第7幕で安全網にした発見オラクル先行のテスト、設計を縛りかねない docstring、そして最初から共に作ってきた AI——これらにも、「残す理由を持てるものだけ残す」という同じ規律を向けます。次の第8幕は、その道具を統制する話です。

次: 第8幕 — 削る・畳む・作らない:道具を統制する(準備中)
