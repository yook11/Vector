# Architecture

Vector は、海外テックニュースを自動収集し、AI で翻訳・要約・分析して投資判断を助けるダッシュボードです。個人開発のプロジェクトで、コードの多くは AI との協働で書いています。

本書は「各構成をなぜそう決めたのか」を、検討した代替案と受け入れたトレードオフとともに記録します。個々の判断の正本は [ADR](adr/README.md) に、判断に至る経緯は [docs/design-journey/](design-journey/) にあります。本書はそれらを設計テーマで束ねて見渡すための入口です。

## 設計で大事にしていること

本アプリの設計判断は、おおむね次の 3 つを大事にして選んでいます。以降の判断は、この 3 つに紐づけて読めます。

1. **どこかが破られても、被害を最小限にとどめる** — 攻撃や侵害で 1 か所が突破されても、その被害をできるだけ狭い範囲に閉じ込める。
2. **扱う領域ごとに責任を分ける** — ニュースの収集・AIによる分析といった異なる関心事を 1 つに混ぜず、それぞれが 1 つの役割だけを担うように境界を引く。境界をまたいで責任が混ざらないようにする。
3. **持続可能な設計を目指す** — 問題が起きたときに、気づいて直せるようにする。長く運用しても、無理なく手を入れられる状態を保つことを意識する。

## 制約

判断はいずれも次の制約の下での選択です。

- 個人開発で、運用に割ける時間と費用に上限があります。
- 開発当初はプライベートリポジトリかつ GitHub Free で、branch protection / required checks が使えませんでした。「テストが通るまではマージできない」を GitHub に強制させられないため、検証の強制力を CI 側で自前に組む前提から始めています(有料プラン移行後の現状は「検証ゲート(CI)」で後述)。
- AI provider(Gemini / DeepSeek)の API 課金コスト。

## あらかじめお伝えしておくこと

- 本番にはデプロイ済みで、パイプライン監査基盤(`pipeline_events`)と工程別の Logfire 計装も稼働しています。ただし監査基盤の投入から日が浅く、「改善前後」を比較できるだけの運用期間がまだ蓄積されていません。そのため失敗率の改善幅といった成果数値は提示せず、該当箇所は「計測成果ではなく設計判断」として記述します。
- CI は GitHub の有料プランで動かしています。現在は `ci-gate` を required status check に指定し、緑でなければマージできないようにしていますが、branch protection を使えなかった当初に、CI が失敗したままマージされた変更が一部履歴に残っています。また月あたりの実行時間(3000 分)を使い切るとワークフローを止めることがあり、その間はチェックが完了せずマージを待つことになります。すべての変更が常に緑の状態でマージできていたわけではない点を、あらかじめお伝えしておきます。

## システム全体図

### Container 構成

本番は Fly.io の 5 app(すべて nrt リージョン)と Neon PostgreSQL で動作します。

```mermaid
flowchart LR
    Browser([Browser])

    subgraph Fly["Fly.io (nrt) — flycast 内部網"]
        direction TB
        FE["vector-frontend<br/>Next.js BFF / Better Auth / proxy<br/>(唯一の public 入口)"]
        RL[("vector-redis-rl<br/>rate limit 専用 / volatile-ttl")]
        CORE["vector-core<br/>FastAPI API + AI workers + scheduler"]
        BROKER[("vector-redis<br/>taskiq broker / AOF 永続")]
        COLLECT["vector-collect<br/>fetch + HTML 抽出 worker<br/>(inbound ゼロ / egress 専用)"]
    end

    News[("外部ニュース源<br/>約45 sources")]
    AI[("AI Provider<br/>Gemini / DeepSeek")]
    DB[("Neon PostgreSQL + pgvector<br/>role: app / auth / collect")]

    Browser -->|HTTPS| FE
    FE -.->|rate limit 判定| RL
    FE -->|短期JWT / flycast| CORE
    CORE -->|enqueue / dequeue| BROKER
    COLLECT -->|dequeue fetch jobs| BROKER
    CORE -->|curation / assessment / embedding| AI
    COLLECT -->|fetch| News
    CORE --> DB
    COLLECT -->|収集系 table のみ| DB
```

ブラウザが到達できるのは `vector-frontend` だけで、backend 以降は flycast の内部網に閉じています。`vector-collect` は外部サイトの HTML を扱うため、AI key を持つ `vector-core` から app ごと分け、両者は `vector-redis` のキュー越しにのみつながります。`vector-redis-rl` はリクエストの後段ではなく、frontend が rate limit を判定するための横の依存です。

## テーマ 1: 同心円の最小権限

Vector で最も危ないのは、外部サイトの HTML を取得して解析する収集処理です。中身を自分で制御できない入力を扱う以上、ここは RCE / SSRF(外部入力を悪用した任意コード実行や、サーバーを踏み台にした内部アクセス)の標的になり得ます。そこでこの 1 点を脅威の起点に置き、「いずれどこかは破られる」前提(assume breach)で、**破られても被害をその層の外へ出さない**ことを設計の軸にしました。

その最初の一手が、app の分割です。AI provider の API キーや BFF の署名鍵を持つ `vector-core` と、外部 fetch だけを担う `vector-collect` を、別々の Fly app に分けています。万一 collect が乗っ取られても、そこには鍵も分析結果も無く、core へは `vector-redis`(taskiq broker)のキュー越しにしか届きません。「危険な処理」と「守りたい資産」を同じ箱に同居させない、という分け方です。

この「1 か所の侵害を、その場で止める」考え方を、app だけでなく DB・Redis・secret・通信経路にも同心円状に重ねています。各層で「単純な作りだと何が漏れるか」を並べたのが次の表です。

| 層 | 分離 | 単純案で足りない理由 |
|----|------|----------------------|
| Fly app | core(AI鍵・BFF鍵を持つ)と collect(外部 fetch のみ)を別 app に | Fly secrets は app 単位でしか分離できず、同一 app 内の process group は全 secret を共有する。collect 侵害で 最重要　の 秘密鍵 が漏れるのを防げない |
| DB role | DB の権限を用途ごとに 3 つ(アプリ用 / 認証用 / ニュース収集用)に分け、各 app 用の権限で接続。ニュース収集用には、収集で使うテーブルへの必要最小限の操作だけを許可。監査ログは書き込みだけ許し、記録した中身は読み返せないようにしている(改ざん・証跡消しの防止)。テーブル定義を変える migration は専用の owner ロールで実行し、通常運用の権限とは分離 | 全 app が同じ権限で DB に繋ぐと、何か一つ が乗っ取られた瞬間に本来そのappに関係のない情報まで読み書きされてしまう。用途ごとに分けておけば、何か一つ が破られても触れるのは最小限のテーブルだけで止まる |
| Redis | collect 専用のユーザーを作り、収集工程で使うキュー(metadata / content)の読み書きと、処理結果の書き込みだけを許可。全消去(`FLUSHALL`)や設定変更(`CONFIG`)などの危険な操作は禁止 | 全 app が共有パスワードの単一ユーザーだと、collect が乗っ取られただけで、他工程のキューや AI の予算・レート制限カウンタまで触れてしまう |
| Secret | frontend↔backend で双方向に使う 2 つのシークレット(ログイン情報の署名 / キャッシュ更新の認証)を、用途ごとに別々の値へ分割。起動時に弱い値や両者が同値なら起動を止める | 2 つは危険度が大きく違う。キャッシュ更新の悪用は軽微だが、署名鍵が漏れると偽の管理者トークンを作られて backend を乗っ取られる。1 つに束ねると、軽い方のキャッシュ用が漏れただけで重い方の backend 乗っ取りまで一緒に開いてしまう。分けておけば、漏れても被害はその用途だけに収まる |
| Transport | backend の公開アドレスを廃止し、Fly の内部ネットワークからのみ到達できるようにした | backend が公開アドレスを持ったまま JWT 検証だけで守ると、検証にバグがある・署名鍵が漏れる、のどちらか 1 つで、インターネットから誰でも直接 backend を叩けてしまう。壁が JWT 1 枚だと、そこが破れた瞬間に全公開と同じになる。内部網に隠せば、JWT が破れても、まず内ネットワークへ侵入しない限り backend には届かない |

この結果、backend・DB・Redis に届くのは frontend 経由だけになりました。本番では接続先が内部ネットワークの住所でなければ起動を止めるので、開発用(localhost など)を誤って設定しても、こっそり動かず即座に起動失敗で気づけます。

では、その「frontend 経由」は中身をどう守っているか。**ブラウザは backend を直接は叩きません。** 唯一の公開入口である frontend(BFF)が、Better Auth の httpOnly Cookie セッションを検証し、本人情報(user_id とロール)を**署名した短期 JWT** に変えて backend へ渡します。backend は署名を検証し、正しいものだけを「frontend が認証済みとして送ってきたリクエスト」として信じます。この JWT は有効期限を 1 分未満に絞り、発行元と宛先(issuer / audience)も固定しているため、万一漏れても、ほぼ即座に期限切れになるうえ、宛先である backend 以外では弾かれます。盗んでも悪用できる隙がほとんど残らないと思います。

加えて、**認証データは別のスキーマ(`auth`)に隔離**しています。認証まわりのテーブルは Better Auth が、アプリ側のテーブルは Alembic(マイグレーション)が管理しており、同じ場所に混ぜると管理がぶつかります。スキーマを分けることで、アプリ用の権限からは認証テーブルに触れず(必要な箇所だけ FK で `auth.user` を指す)、認証情報をアプリ領域から構造的に切り離せます。

> これらの脅威は、Claude Code で red-team エージェントを組み、本リポジトリへ敵対的レビューをかけて洗い出しました。ここでの分離はいずれも、実害を観測したからではなく、攻撃対する予防として設けています。

## テーマ 2: 不正状態を作れない構造

Vector のパイプラインには、「この記事は分析してよい状態か」「この工程は、渡された対象を処理できる状態か」「処理結果は DB に保存してよい形式か」といった前提条件が多くあります。素朴に書くなら、Service や Task のあちこちで `if` を置いて毎回確認することになります。けれど確認が散るほど、呼び出し側は常に「この値は本当に検証済みか」を疑わなければならず、書き忘れも起きやすくなります。

このテーマで選んだのは、**前提条件を呼び出し側の注意ではなく、型と DB 制約に移す**ことでした。入口で一度だけ確認し、通ったものを「条件を満たした型」に変換する。以降の工程は、その型を受け取った時点で前提条件が成立しているものとして扱います。中心にあるのは型安全そのものではなく、**処理を始めてよい証明をどこで確定させるか**です。

具体的には、Service や Task に散りがちな確認を、次の 4 つの構造へ移しています。

- **記事の状態を毎回 `if` で見ない** — 最初は、収集候補・本文が揃った記事・AI で整えた途中成果・ユーザーに見せる分析済み記事を、一つの「記事」概念として扱っていました。けれど状態ごとに必要な値が違うため、一つの箱に寄せると nullable な列が増え、使う側が「この時点で本文はあるか」「分析結果はあるか」を毎回確認することになります。そこで、未完成記事・分析可能記事・curation・分析済み記事を別の型やテーブルに分け、対象を持っている時点で、その状態に必要な値が揃っていることを表せるようにしました。
- **工程を開始できるかを Service 内で確かめ直さない** — 各工程は、前工程から「次へ進める」という合図を受け取るのではなく、自分の開始時点で DB 事実を読み直します。対象が存在し、まだ未処理で、必要な値が揃っていると確認できたときだけ `ReadyFor...` 型を作ります。Service は `Ready` を受け取ったら、キュー状態や claim 状態を知らずに処理本体へ進めます。
- **値の形式が正しいかを利用箇所で毎回見ない** — `CategorySlug` や URL 系の値は、値オブジェクトを通ったものだけを扱います。DB には `CHECK` 制約を最後の砦として置き、アプリ側のバグや別経路の書き込みでも不正な形式が残らないようにします。
- **識別子の整合性を読む側で確認しない** — 同じ識別子を別テーブルにも持つ場合、composite FK で食い違い(drift)を DB が拒否します。整合性を利用箇所の `if` に任せず、保存できるデータの形として固定します。

この設計を素直に書けるようにするために、モデル層は SQLModel から SQLAlchemy 2.0 の DeclarativeBase へ移行しました。

SQLModel は立ち上げの速さには優れています。Pydantic schema と ORM model を一つのクラスで書けるため、小さく始める段階では便利でした。一方で Vector では、API の request / response schema は最初から専用の Pydantic schema として分けています。DB model をそのまま API に露出しない方針だったため、SQLModel の「model = schema」という利点はあまり残っていませんでした。

逆に、必要になっていたのは「保証を DB と ORM に正しく書けること」でした。`ondelete`、複合 index、`server_default`、partial index、`CHECK`、composite FK といった DB 制約を SQLModel の `Field()` だけで表すのは難しく、結局 `sa_column=Column(...)` で SQLAlchemy へ逃げる箇所が増えていました。値オブジェクトも ORM 層に自然に載せられず、models では `str` に戻り、service 層で手動変換する状態になっていました。

SQLAlchemy 2.0 の DeclarativeBase に移したことで、`Mapped[CategorySlug]` のように models 層へ値オブジェクトの型を残し、`TypeDecorator` と `type_annotation_map` で DB 読み書き時の変換を一箇所へ寄せられるようになりました。DB 制約も SQLAlchemy の記法で統一して書けるため、「型では防ぐが DB では防がない」「DB にはあるが Python 側ではただの文字列」というずれを減らせます。

受け入れたトレードオフもあります。SQLModel の簡潔さは失われ、API schema と ORM model は明示的に分けて書く必要があります。ただ Vector では、長い非同期パイプラインを AI と協働しながら保守していく以上、「毎回気をつけてチェックする」よりも「不正な状態を作りにくい構造にする」ことの価値が大きいと判断しました。

このテーマは、抽象を増やしたいという話ではありません。むしろ、チェックを増やすのではなく、**チェックが必要な場所を減らす**ための選択です。詳細な判断は [SQLModel → SQLAlchemy DeclarativeBase 移行](adr/sqlmodel-to-declarative-migration.md)、[値オブジェクト + SQLAlchemy Declarative 移行](adr/value_objects_sqlalchemy_migration.md)、判断に至る過程は [第2幕: 値オブジェクト](design-journey/02-value-objects.md) と [第6幕: ドメインモデル再構築](design-journey/06-domain-model-rebuild.md) に残しています。

## テーマ 3: 黙って消える失敗の可視化

HTTP リクエストの失敗はユーザーに 500 が返ります。しかし worker の失敗は画面に出ません。記事が次工程に進まない、週次処理が動かない、在庫が溜まる、という形で静かに残ります。実際に過去には、ある収集工程が長時間止まっていたことに気づけませんでした。

そこで Vector では、非同期パイプラインの各段で起きたことを、append-only の監査ログ `pipeline_events` に記録するようにしました。目的はログを増やすことではありません。**黙って消える失敗を、あとから SQL で追える事実に変える**ことです。設計の第一原理は、「すべてを忘れた未来の読み手が、この 1 行を見て何が起きたかを SQL で再構成できるか」でした。

この方針は、次の 4 つの判断に分かれます。

- **最新状態ではなく、起きた事実を残す** — 業務テーブルに `failure_reason` のような列を足すだけでは、最新状態しか残りません。何度失敗したのか、先週は動いていたのか、どの工程から止まったのかが消えてしまいます。そこで、非同期パイプラインの各段を「1 行 = 1 イベント」として immutable に記録し、時系列で追える形にしました。
- **成功と失敗で、監査の書き込み境界を分ける** — 成功 / skip の監査 INSERT は業務 state 更新と同一トランザクションに置き、「監査行が焼けた = 業務が確定した」を DB レベルで保証します。一方、失敗時は業務 tx が rollback された後に別 session・別 tx で best-effort に焼きます。永続化に失敗したという記録まで同じ rollback で消える矛盾を避けるためです。
- **失敗の語彙を、原因・retry 可否・処理方針に分ける** — 「何が起きたか」「同じ入力を再実行して変わりうるか」「記事を消すか」を 1 つの分類 enum に詰めると、直らない例外が retry 中だけ `retryable` と記録され、アラートが最終試行まで黙るような嘘が生まれます。そこで `outcome_code` / `retryability` / `failure_action` などの直交した属性に投影し、後から集計しても事実と処理方針が混ざらないようにしました。
- **監査を制御状態にしない** — `pipeline_events` は「次に何をするか」を決めるテーブルではなく、発生時点の事実の witness です。再試行 / drop / keep の判断は現在状態や専用の制御テーブルに置き、監査は immutable に保ちます。Logfire は補助 telemetry として、span duration、工程別の例外、メモリ逼迫の予兆などを見る役割に分けています。

この仕組みによって、失敗は「気づけなかった沈黙」から「どの工程で、何が、なぜ起き、記事がどう扱われたかを後から調べられる事実」になりました。壊れないことを保証するためではなく、壊れたときに見つけて直せる状態を作るための設計です。

`pipeline_events` と工程別の Logfire 計装は本番で稼働しています。ただし投入から日が浅く、失敗率改善などの運用数値はまだ提示できません。本書では、これは計測成果ではなく設計判断として記述します。

詳細は [ADR-008(pipeline_events 監査)](adr/008_pipeline_events_audit.md)、[Pipeline Events Design](observability/pipeline-events-design.md)、[Failure Attributes](observability/pipeline-events-failure-attributes.md)、[Error Visibility](observability/error-visibility.md)、[Memory Monitoring](observability/memory-monitoring.md) を参照してください。

## テーマ 4: 通常操作を壊さない防御の粒度

rate limit は、弱いと攻撃を止められません。けれど強くしすぎると、普通に使っているユーザーまで止めてしまいます。実際、最初の frontend proxy rate limit は、静的アセット以外の全リクエストを単一の `rl:ip:<ip>` 60 req/min で数えていました。その結果、リロードして数回画面遷移するだけで、429 Too Many Requests が返ることがありました。

原因は、Next.js が画面表示やリンク先の先読みのために自動で出すリクエストまで、ユーザー操作や攻撃的な連続アクセスと同じ上限で数えていたことです。ユーザーから見ると 1 回の画面表示でも、裏側では page GET、RSC、prefetch、API がまとまって発生します。これらを全部 1 つの IP 上限に入れると、攻撃ではない通常操作まで上限に達してしまいます。

ここで単に上限値を上げるだけでは、根本的な解決になりません。prefetch の誤 429 は減りますが、ログイン試行、変更系 request、RSC flood まで同じように緩くなってしまうためです。問題は「60 req/min が低すぎたこと」だけではなく、性質の違う request を同じ財布で数えていたことでした。

そこで frontend の rate limit は、request の性質と識別子ごとに、別々の上限で数える形へ再構成しました。

- **prefetch は止めすぎず、完全には除外しない** — `_rsc` GET は Next.js が自動で出すため、通常操作でも増えやすく、通常の読み取りと同じ上限に入れると誤 429 の原因になります。そこで専用の `rl:rsc:<ip>` に分け、上限を寛容にしました。一方で、完全に rate limit から外すと、認証済み cookie を使った RSC 連打で backend や DB pool に負荷をかけられるため、専用枠で数え続けます。
- **認証済み request は session と IP の両方で見る** — proxy はアプリケーション側の最初の門番として、ページや API の本処理に入る前に rate limit を判定します。ただしこの時点では session cookie の正当性をまだ検証していないため、cookie 値だけを key にすると、偽造 cookie を変えるだけで制限を回避できます。そのため `rl:sess:<hash>` による session 単位の上限と、`rl:ip:<ip>` による IP 単位の上限を併用し、どちらか一方でも超過したら止めます。
- **読み取りと変更系を同じように扱わない** — GET / HEAD / OPTIONS は読み取りとして扱い、POST / PUT / PATCH / DELETE のような変更系とは分けます。IP が取れない異常経路でも、読み取りは fail-open して通し、匿名の変更系だけは `rl:uwrite:global` で最低限制限します。CORS preflight や health check まで変更系扱いにすると、この共有枠を不要に消費してしまうため、HEAD / OPTIONS は読み取り側に寄せています。
- **rate limit 用 Redis 障害では、通常閲覧を止めない** — frontend 入口の proxy.ts は、Redis に保存したカウンターを見て rate limit を判定します。Redis が落ちると「この IP が何回目か」を判断できません。ここで fail-closed にすると、判定できないという理由だけで通常閲覧やログイン画面まで止まり、Redis 障害がそのままサービス停止になってしまいます。そこで一般の proxy rate limit は fail-open にし、警告ログを出して通します。

一方で、ログイン試行制限は別扱いです。パスワード入力の失敗回数を数えられない状態でログイン試行を通し続けると、総当たり攻撃や辞書攻撃のようなログイン試行の連打を許してしまいます。以前はこの回数も Redis に保存していましたが、Redis エラー時に「失敗履歴なし」と扱われ、制限が効かなくなる穴がありました。そこで Better Auth のログイン limiter は保存先を DB に移し、rate limit 用 Redis が落ちてもログイン試行制限が無制限に開かないようにしました。

同じ考え方で、弱い secret、誤った内部 URL、非 TLS の DB 接続のように、動かすこと自体が危険な設定は起動時に止めます。一方で、通常閲覧の rate limit Redis 障害のように、止めることがサービス停止に直結するものは警告を出して通します。防御を一枚岩にせず、止めるべき失敗と通してよい失敗を分けることを重視しています。

## 非同期パイプライン

API サーバーは、保存済みのニュース・分析結果を返す責務に寄せています。ニュース収集、本文抽出、AI 分析、embedding、trend discovery、週次ブリーフィング、maintenance などの重い処理は、API レスポンスの完了条件に含めず、taskiq + Redis Streams の worker pipeline 上で処理しています。

外部 fetch や LLM 呼び出しを同期処理に置くと、API の遅延、timeout、外部サービス障害の伝播につながるためです。ユーザー向け API と生成・分析処理を分離することで、外部依存の揺らぎが dashboard 表示全体に波及しにくい構成にしています。

pipeline は、収集、本文抽出、AI 分析、embedding、trend discovery などの段階ごとに task / queue を分けています。複数の責務を 1 つの worker task に詰め込まず、各段階が成功したときに次の task を `.kiq()` で明示的に投入する task chaining にしています。これにより、段階ごとのスケール、リトライ、失敗箇所の特定、再実行範囲の制御をしやすくしています。

Redis Streams の message を別 worker が検知して次工程を投入する event-driven 方式も取り得ましたが、この pipeline では基本的に次工程が一意に決まるため、明示的な task chaining を選びました。routing logic や subscriber を増やさず、処理の流れをコード上で追いやすくするためです。

一時的な失敗や途中で止まった処理は、定期実行される maintenance task が検出し、対象レコードの状態に応じて必要な queue に再投入します。単純に全工程をやり直すのではなく、失敗した段階から再開できるようにしています。cron と worker が別プロセスになる運用コストは発生しますが、Fly.io の process group で吸収しています。

再投入は放置すると API 使用量や queue 滞留が膨らむため、stage ごとの kill switch・日次予算・hold gate の 3 段で制御しています。日次予算は Redis 上で atomic に更新し、複数 worker が同時に動いても上限を超えません。問題が起きたときは工程単位で止められるため、pipeline 全体を止めずに影響範囲を絞れます。また、記事の物理削除は provider が明示的に「処理対象外」と拒否したときだけに限定し、API key の設定ミスや一時障害を品質問題と誤認して大量削除するのを防いでいます。

タスクキューには taskiq を採用しました。arq は maintenance-only に入っていたため、新規採用では避けています。AI provider は環境変数による切り替えにはせず、composition root で固定配線しています。curation / embedding は Gemini、assessment / 週次ブリーフィングは DeepSeek に固定し、共有 env の設定ミスで工程ごとの provider が入れ替わる事故を構造的に防いでいます。provider を切り替える場合は、コード変更と worker 再起動を必要とする設計です。

## 運用して分かったこと / 取り組み中の課題

デプロイして運用する中で気づいた点と、まだ解けていない課題を残します。

**(解決済み)非 AI worker のメモリ逼迫** — デプロイ後、AI を実行しない scheduler / collect / maintenance のプロセスが、メモリ制限下で落ちることがありました。原因は、各 worker の起動時に AI provider の重い SDK まで一律に読み込んでいたことでした。具体 SDK を各 worker の起動 hook 内で遅延 import し、AI を使う worker にだけ常駐させることで、非 AI プロセスのメモリ使用量を抑えました。

**(取り組み中)成功率メトリクスの分母をどう定義するか** — 工程ごと(curation / assessment / embedding など)の成功率をメトリクスとして出し始めていますが、その分母の定義をまだ決めきれていません。迷っているのは、インフラ起因の失敗(DB や Redis の一時障害、AI provider の quota 超過・タイムアウト)を分母に含めるかです。含めると、パイプラインのロジックや AI 出力の品質ではなく、インフラの揺らぎで成功率が上下します。除外すると、可用性の問題を見落とす恐れがあります。また、1 記事が複数回リトライされるため、「試行回数」で数えるか「記事単位の最終結果」で数えるかでも値が変わります。`pipeline_events` には原因コード・retry 可否・処理方針を記録しているので、いまは「retry 可否で切り分け、最終的に非 retryable で終わった記事だけを"失敗"の分母に入れ、インフラ起因の transient な失敗は別の信頼性指標として分けて見る」方向を検討しています。ただし、記事単位とリトライ単位の数え方、DB / Redis 障害をどの指標に寄せるか、どの粒度(工程別 / 原因別)で可視化するかは、まだ整理しきれていません。

AIのバッチ処理の導入

アサートのエラーの可視化



**(既知の残リスク)** — 伏せずに記録します。

- migration ゲートの破壊的操作判定に、複文(`SET ...; DROP ...`)を誤って許可しうる fail-open が残っています(follow-up)。
- DB-backed のログイン limiter は best-effort で、DB の部分障害では fail-open しうる残リスクがあります。

## ローカル環境

ローカルは Docker Compose で全 app をまとめて起動し、公開するのは frontend のポートだけにしています。本番の「frontend が唯一の public 入口」という境界を、開発環境でも同じ形で再現しています。

## 検証ゲート(CI)

CI の目的は、失敗を早く・安く見つけ、危険な変更を本番前に止めることです。検証は、走るタイミングの違う三層に分けています。

- **ローカル pre-commit(一次防衛)** — commit 時に手元で、秘密情報の混入(gitleaks)、Dockerfile lint(hadolint)、lint / format(ruff / biome)、基本 hygiene を確認します。速くて頻繁な検査を最前段に置き、大半をここで弾きます。ただし `--no-verify` で迂回できるため、最終防衛ではありません。
- **PR / main push の blocking gate(最終ゲート)** — 品質ゲート(ruff、pytest の unit / integration、biome、tsc、vitest、alembic head 検査)を変更領域ごとに出し分け、全テストを `ci-gate`(表示名 "CI gate")という単一 job に集約します。並行して、依存ライブラリの既知脆弱性(osv-scanner / npm audit)とソースコードの静的解析(Semgrep, OWASP Top 10)を blocking で検査します。pre-commit で迂回され得る gitleaks / hadolint は、この層で再実行して二重化します。
- **nightly(深い背景検査)** — マージは止めず、毎晩走らせて通知に回す重い検査です。コンテナ・インフラ設定の脆弱性(Trivy: 依存 CVE と Dockerfile / docker-compose の misconfig)と、OpenAPI 仕様に対する property-based fuzzing(Schemathesis: 未処理の 5xx 露出、宣言外の status code、response schema との不一致)を見ます。

検査ツールは、守備範囲が重ならないように選んでいます。gitleaks = 秘密の漏れ、Semgrep = コード自体の脆弱性、osv-scanner / npm audit = 依存ライブラリの既知脆弱性、Trivy = 土台(コンテナ・インフラ設定)の脆弱性、Schemathesis = 実際に叩いて仕様と実装の乖離、という分担です。

新しい検査は、いきなり blocking にすると既存の指摘で全部赤くなり、誰も通れません。そこで、**warn-only で導入 → 指摘を triage(コードか仕様を直す)→ main が 0 件になったのを確認 → blocking に昇格**、という同じ段階を踏んでいます。退路として、blocking 化後に問題が出たら一時的に warn-only へ戻すことも許容します。

### 強制力をどう持たせるか

開発当初はプライベートリポジトリかつ GitHub Free で、branch protection / required checks が使えませんでした。「テストが緑になるまでマージできない」を GitHub に強制させられないため、代替として全テストを `ci-gate` 1 個に集約し、本番デプロイを `ci-gate` の成功だけに依存させる(個別 job の先走りを 1 ゲートで束ねる)形にしていました。スキャン結果も、画面に SARIF を出せない(GitHub Advanced Security が Free では無効)ため、Actions Artifacts に退避してローカルで triage しています。

その後、有料プランへ移行し、プライベートのままでも branch protection / required checks が使えるようになりました。そこで `ci-gate` をそのまま required status check に指定しています。Free 時代に「代替」として作った集約 job が、無改造で「本物の必須チェック」になり、緑でなければマージできないことを GitHub 側で強制できます。

サプライチェーン面では、GitHub Actions の参照をすべて 40 桁の commit SHA で固定しています。2026 年 3 月に起きた人気 Action のタグ改ざん(供給元のすり替え)を踏まえ、可変な tag 参照を禁止し、改ざんに気づかず取り込む経路を塞いでいます。

検査範囲には既知の限界もあります。Schemathesis は現状 GET のみで、変更系(POST / PUT / DELETE)や stateful なシナリオは未カバーです。E2E smoke も匿名フローに限られ、認証後の watchlist / search はローカル検証に留めています。

## 関連ドキュメント

- [ADR Index](adr/README.md) — 各設計判断の正本(context / 代替案 / トレードオフ)
- [docs/design-journey/](design-journey/) — 判断に至る経緯のナラティブ
- [Pipeline Events Design](observability/pipeline-events-design.md) / [Failure Attributes](observability/pipeline-events-failure-attributes.md)
- [Error Visibility](observability/error-visibility.md) / [Memory Monitoring](observability/memory-monitoring.md)
