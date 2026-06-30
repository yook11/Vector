# Architecture

Vector は、海外テックニュースを自動収集し、AI で翻訳・要約・分析して投資判断を助けるダッシュボードです。個人開発のプロジェクトで、コードの多くは AI との協働で書いています。

本書は「各構成をなぜそう決めたのか」を、検討した代替案と受け入れたトレードオフとともに記録する、**設計判断の正本**です。判断に至る経緯や設計思想の変化は [docs/design-journey/](design-journey/) に、AI とどう協働して作っているかは [how-i-build-with-ai](how-i-build-with-ai.md) に分けています。本書は設計テーマで束ねて全体を見渡す入口でありつつ、構造と判断理由については本書自身が一次情報です。

## 設計で大事にしていること

本アプリの設計判断は、おおむね次の 3 つを大事にして選んでいます。以降の判断は、この 3 つに紐づけて読めます。

1. **どこかが破られても、被害を最小限にとどめる** — 攻撃や侵害で 1 か所が突破されても、その被害をできるだけ狭い範囲に閉じ込める。
2. **扱う領域ごとに責任を分ける** — ニュースの収集・AIによる分析といった異なる関心事を 1 つに混ぜず、それぞれが 1 つの役割だけを担うように境界を引く。境界をまたいで責任が混ざらないようにする。
3. **持続可能な設計を目指す** — 問題が起きたときに、気づいて直せるようにする。長く運用しても、無理なく手を入れられる状態を保つことを意識する。

## 制約と前提

判断はいずれも次の制約の下での選択です。

- 個人開発で、運用に割ける時間と費用に上限があります。設計の規模はこの制約の中で「学習として投資する価値があるか」を都度判断して選んでおり、過剰になっていないかは常に気にしています。
- 開発当初はプライベートリポジトリかつ GitHub Free で、branch protection / required checks が使えませんでした。検証の強制力を CI 側で自前に組む前提から始めています(現状は「検証ゲート(CI)」で後述)。
- AI provider(Gemini / DeepSeek)の API 課金コスト。

あらかじめお伝えしておくことが 2 点あります。

- 本番にはデプロイ済みで、パイプライン監査基盤(`pipeline_events`)と工程別の Logfire 計装も稼働しています。ただし監査基盤の投入から日が浅く、「改善前後」を比較できるだけの運用期間がまだ蓄積されていません。そのため失敗率の改善幅といった**成果数値は本書では提示せず**、該当箇所は「計測成果ではなく設計判断」として記述します。
- branch protection を使えなかった当初に、CI が失敗したままマージされた変更が一部履歴に残っています。また月あたりの実行時間を使い切るとワークフローを止めることがあり、その間はチェックの完了を待ちます。すべての変更が常に緑の状態でマージできていたわけではない点を、先にお伝えしておきます。

## システム全体図

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

## テーマ 1: 防御の設計

Vector で最も危ないのは、外部サイトの HTML を取得して解析する収集処理です。中身を自分で制御できない入力を扱う以上、ここは RCE / SSRF(外部入力を悪用した任意コード実行や、サーバーを踏み台にした内部アクセス)の標的になり得ます。そこでこの 1 点を脅威の起点に置き、「いずれどこかは破られる」前提(assume breach)で防御を設計しました。軸は 2 つです。**①破られても被害をその層の外へ出さない(最小権限)**、**②防御の粒度を分け、止めるべき失敗と通してよい失敗を混ぜない**。

### 破られても被害を閉じ込める

その最初の一手が、app の分割です。AI provider の API キーや BFF の署名鍵を持つ `vector-core` と、外部 fetch だけを担う `vector-collect` を、別々の Fly app に分けています。万一 collect が乗っ取られても、そこには鍵も分析結果も無く、core へは `vector-redis`(taskiq broker)のキュー越しにしか届きません。「危険な処理」と「守りたい資産」を同じ箱に同居させない、という分け方です。

この「1 か所の侵害を、その場で止める」考え方を、app だけでなく DB・Redis・secret・通信経路にも同心円状に重ねています。各層で「単純な作りだと何が漏れるか」を並べたのが次の表です。

| 層 | 分離 | 単純案で足りない理由 |
|----|------|----------------------|
| Fly app | core(AI鍵・BFF鍵を持つ)と collect(外部 fetch のみ)を別 app に | Fly secrets は app 単位でしか分離できず、同一 app 内の process group は全 secret を共有する。collect 侵害で最重要の秘密鍵が漏れるのを防げない |
| DB role | DB の権限を用途ごとに 3 つ(アプリ用 / 認証用 / ニュース収集用)に分け、各 app 用の権限で接続。収集用には収集テーブルへの必要最小限の操作だけを許可。監査ログは書き込みだけ許し、記録した中身は読み返せないようにする(改ざん・証跡消しの防止)。テーブル定義を変える migration は専用の owner ロールで実行し、通常運用の権限と分離 | 全 app が同じ権限で繋ぐと、何か一つが乗っ取られた瞬間に本来無関係な情報まで読み書きされる。用途ごとに分けておけば、破られても触れるのは最小限のテーブルだけで止まる |
| Redis | collect 専用ユーザーを作り、収集工程で使うキュー(metadata / content)の読み書きと処理結果の書き込みだけを許可。全消去(`FLUSHALL`)や設定変更(`CONFIG`)などの危険な操作は禁止 | 全 app が共有パスワードの単一ユーザーだと、collect 侵害だけで他工程のキューや AI 予算・レート制限カウンタまで触れてしまう |
| Secret | frontend↔backend で双方向に使う 2 つのシークレット(ログイン情報の署名 / キャッシュ更新の認証)を、用途ごとに別々の値へ分割。起動時に弱い値や両者が同値なら起動を止める | 2 つは危険度が大きく違う。キャッシュ更新の悪用は軽微だが、署名鍵が漏れると偽の管理者トークンを作られて backend を乗っ取られる。1 つに束ねると、軽い方が漏れただけで重い方まで一緒に開く |
| Transport | backend の公開アドレスを廃止し、Fly の内部ネットワークからのみ到達できるようにした | 公開アドレスを持ったまま JWT 検証だけで守ると、検証バグか署名鍵漏洩のどちらか 1 つで誰でも直接 backend を叩ける。内部網に隠せば、JWT が破れても内ネットワークへ侵入しない限り届かない |

この結果、backend・DB・Redis に届くのは frontend 経由だけになりました。本番では接続先が内部ネットワークの住所でなければ起動を止めるので、開発用(localhost など)を誤って設定しても、こっそり動かず即座に起動失敗で気づけます。

**ブラウザは backend を直接は叩きません。** 唯一の公開入口である frontend(BFF)が、Better Auth の httpOnly Cookie セッションを検証し、本人情報(user_id とロール)を**署名した短期 JWT**(有効期限 1 分未満、issuer / audience 固定)に変えて backend へ渡します。backend は署名を検証し、正しいものだけを信じます。漏れてもほぼ即座に期限切れになり、宛先である backend 以外では弾かれます。

この BFF パターンは、消去法ではなく次の代替案と比べて選びました。**(a) JWT を backend で直接検証**は署名鍵の共有・失効遅延・クロック同期の問題があり、**(b) backend が認証 DB を直接参照**は backend のステートレス性を壊して密結合になり、**(c) ブラウザから直接 API を叩く + CORS**はシークレット漏洩と認証の一元管理不可を招きます。BFF は、全 API コールが単一プロキシ経路を通るため監査が一点に集まり、backend をステートレスに保てて水平スケールできる点を採りました。トレードオフは、frontend→backend に 1 ホップ増えること(内部網なので 1ms 未満)と、プロキシが単一障害点になること(障害時は全 API 停止、再起動ポリシーで緩和)です。

加えて、**認証データは別スキーマ(`auth`)に隔離**しています。単一スキーマに混ぜると Better Auth CLI と Alembic がマイグレーション履歴で競合し、別 DB インスタンスに分けるのは運用コストが過大でした。同一 DB 内のスキーマ分離は PostgreSQL ネイティブの機能で回避策が要りません。認証テーブル(user / session / account / verification)は Better Auth CLI が、アプリ側は Alembic が管理し、Alembic 側は auth スキーマを autogenerate の対象から除外しています。アプリ用の権限からは認証テーブルに触れず、必要な箇所だけ FK で `auth.user` を指します(例: `watchlist_entries.user_id` → `auth.user.id`)。認証を将来 Auth0 等の外部サービスへ移しても、アプリ側のスキーマには影響しません。

> これらの脅威は、Claude Code で red-team エージェントを組み、本リポジトリへ敵対的レビューをかけて洗い出しました(やり方は [how-i-build-with-ai](how-i-build-with-ai.md))。ここでの分離はいずれも、実害を観測したからではなく、攻撃に対する予防として設けています。

### 止める失敗と通す失敗を分ける

防御は、弱いと攻撃を止められませんが、強くしすぎると普通に使っているユーザーまで止めてしまいます。最初の frontend proxy rate limit は、静的アセット以外の全リクエストを単一の `rl:ip:<ip>` 60 req/min で数えていました。その結果、リロードして数回画面遷移するだけで 429 が返ることがありました。Next.js は 1 回の画面表示の裏で page GET・RSC・prefetch・API をまとめて出すため、これらを 1 つの IP 上限に入れると通常操作が上限に達します。

ここで単に上限値を上げるだけでは根本解決になりません。prefetch の誤 429 は減りますが、ログイン試行や変更系まで一緒に緩くなるためです。問題は「60 が低すぎた」ことだけでなく、性質の違う request を同じ財布で数えていたことでした。

背景には構造的な事情もありました。proxy は session を直接検証するため Better Auth が `/api/auth/*` に持つ built-in rate limit を通らず、かつ session を毎回キャッシュせず検証する設計(後述)のため、認証済み Cookie 1 つでの RSC 連打が全て DB アクセスに直行し、放置すれば DB 接続プールを枯らして frontend 全体を止め得ます。

そこで rate limit を、request の性質(prefetch / 読み取り / 変更系)と識別子(session / IP)ごとに別々の上限で数える多層構成へ再構成しました。

- **prefetch(`_rsc`)は専用枠で寛容に数え、完全には除外しない** — 通常操作で増えやすいので別枠にしますが、完全に外すと認証済み Cookie での RSC 連打を許すため、寛容な上限で数え続けます。
- **認証済み request は session と IP の両方で見る** — proxy は session Cookie の正当性をまだ検証していない段階で判定するため、Cookie 値だけを key にすると偽造で回避できます。session 単位と IP 単位を併用し、どちらか一方でも超過したら止めます。
- **読み取りと変更系を同じ財布にしない** — GET / HEAD / OPTIONS は読み取り、POST / PUT / PATCH / DELETE は変更系として分けます。IP が取れない異常経路でも、読み取りは fail-open で通し、匿名の変更系だけ最小限の上限で縛ります。

上限値は本番計測をもとに環境変数で調整する前提にしています。設計上の要は、**storage(Redis)と識別子ソースで fail の向きを変えている**ことです。Redis が落ちて回数を数えられないときは fail-open で通します(ここで止めると Redis 障害がそのままサービス停止になるため)。一方、信頼できる IP ソース(Fly が必ず付与し偽装できないヘッダ)が欠けた場合は、production では別ソースに fallback せず unknown として扱う fail-closed にします(欠如自体が経路の異常だからです)。

このとき、認証済みセッションを毎回キャッシュせず検証し直すのは意図的な判断です。キャッシュすれば DB アクセスは減りますが、管理者権限の剥奪やセッション無効化が反映されるまで時間差が生まれ、その間は失効済みの権限が通ってしまいます。DB 負荷の削減は、キャッシュではなく上記の rate limit と接続プール設定で受けることにしました。

**ログイン試行制限だけは別扱いで、fail-open を許しません。** パスワード失敗回数を数えられない状態で通し続けると、総当たり攻撃を許します(OWASP API Security でも認証の brute-force 制限は fail-open しないことが求められます)。以前はこの回数も rate limit 用 Redis に保存しており、Redis エラー時に「失敗履歴なし」と扱われて制限が外れる穴がありました。そこで Better Auth のログイン limiter は保存先を DB(別の障害ドメイン)へ移し、rate limit 用 Redis が落ちても無制限には開かないようにしました。これは厳密な上限ではなく best-effort です(DB 側が読み込み→判定→書き込みで、アトミックな加算ではないため高並行下では取りこぼし得ます)。より厳密な制限が要る脅威が出たら CAPTCHA やアカウントロックを重ねますが、現時点ではこの規模に対して過剰と判断し見送っています。

同じ「止める失敗と通す失敗を分ける」考え方で、弱い secret・誤った内部 URL・非 TLS の DB 接続のように**動かすこと自体が危険な設定は起動時に止め**、通常閲覧の rate limit Redis 障害のように**止めることがサービス停止に直結するものは警告を出して通します**。防御を一枚岩にしないことを重視しています。

## テーマ 2: 不正状態を作れない構造

Vector のパイプラインには、「この記事は分析してよい状態か」「この工程は、渡された対象を処理できる状態か」「処理結果は DB に保存してよい形式か」といった前提条件が多くあります。素朴に書くなら Service や Task のあちこちで `if` を置いて毎回確認することになりますが、確認が散るほど、呼び出し側は常に「この値は本当に検証済みか」を疑わねばならず、書き忘れも起きやすくなります。

このテーマで選んだのは、**前提条件を呼び出し側の注意ではなく、型と DB 制約に移す**ことでした。入口で一度だけ確認し、通ったものを「条件を満たした型」に変換する。以降の工程は、その型を受け取った時点で前提条件が成立しているものとして扱います。中心にあるのは型安全そのものではなく、**処理を始めてよい証明をどこで確定させるか**です。

具体的には、Service や Task に散りがちな確認を、次の 4 つの構造へ移しています。

- **記事の状態を毎回 `if` で見ない** — 収集候補・本文が揃った記事・AI で整えた途中成果・分析済み記事を一つの「記事」概念に寄せると、状態ごとに必要な値が違うため nullable な列が増え、使う側が毎回確認することになります。そこで未完成記事・分析可能記事・curation・分析済み記事を別の型やテーブルに分け、対象を持っている時点で必要な値が揃っていることを表せるようにしました。
- **工程を開始できるかを Service 内で確かめ直さない** — 各工程は前工程からの合図を信じるのではなく、自分の開始時点で DB 事実を読み直し、対象が存在し未処理で必要な値が揃っているときだけ `ReadyFor...` 型を作ります。Service は `Ready` を受け取れば、キュー状態や claim 状態を知らずに処理本体へ進めます。
- **値の形式が正しいかを利用箇所で毎回見ない** — `CategorySlug` や URL 系の値は、値オブジェクトを通ったものだけを扱い、DB に `CHECK` 制約を最後の砦として置きます。
- **識別子の整合性を読む側で確認しない** — 同じ識別子を別テーブルにも持つ場合、composite FK で食い違い(drift)を DB が拒否します。整合性を `if` に任せず、保存できるデータの形として固定します。

この設計を素直に書けるようにするため、モデル層は SQLModel から SQLAlchemy 2.0 の DeclarativeBase へ移行しました。

SQLModel は Pydantic schema と ORM model を一つのクラスで書けるため、小さく始める段階では便利でした。一方 Vector では API の request / response schema を最初から専用の Pydantic schema として分けており、DB model をそのまま API に露出しない方針だったため、「model = schema」という利点はあまり残っていませんでした。逆に必要だったのは「保証を DB と ORM に正しく書けること」で、`ondelete`・複合 index・`server_default`・partial index・`CHECK`・composite FK を SQLModel の `Field()` だけで表すのは難しく、`sa_column=Column(...)` で SQLAlchemy へ逃げる箇所が増えていました。値オブジェクトも ORM 層に自然に載らず、models では `str` に戻って service 層で手動変換していました。

DeclarativeBase に移したことで、`Mapped[CategorySlug]` のように models 層へ値オブジェクトの型を残し、`TypeDecorator` と `type_annotation_map` で DB 読み書き時の変換を一箇所へ寄せられるようになりました。その値オブジェクト自体は Pydantic の `RootModel[str]` を frozen で定義し、不変性・等価比較・JSON での生値出力を継承で得ています(手書きだと 1 つあたり約 90 行のボイラープレートが、約 10 行で済みます)。`Annotated[str, ...]` の型エイリアスで済ませる案もありましたが、実体が `str` のままで Pydantic の検証コンテキスト外(service 層やパイプライン内部)では不変条件を強制できず `isinstance` も効かないため、本物の型にしました。

移行は一括ではなく、値オブジェクトを使う Category / Keyword のような「葉」から段階的に進めています。これは構造的な必然でもあって、SQLModel と DeclarativeBase は metadata が別物のため、両者が混在する間は跨いだ ORM Relationship を定義できず(FK でのみ繋がる)、自動命名規約の食い違いで Alembic の autogenerate が差分を出し続けます。各テーブルの移行後に autogenerate の差分がゼロであることを、移行完了の判定にしています。

受け入れたトレードオフもあります。SQLModel の簡潔さは失われ、API schema と ORM model は明示的に分けて書く必要があります。ただ Vector では、長い非同期パイプラインを AI と協働しながら保守していく以上、「毎回気をつけてチェックする」よりも「不正な状態を作りにくい構造にする」ことの価値が大きいと判断しました。このテーマは抽象を増やす話ではなく、**チェックが必要な場所を減らす**ための選択です。

判断に至る過程は [第2幕: 値オブジェクト](design-journey/02-value-objects.md) と [第6幕: ドメインモデル再構築](design-journey/06-domain-model-rebuild.md) に残しています。

## テーマ 3: 黙って消える失敗の可視化

HTTP リクエストの失敗はユーザーに 500 が返ります。しかし worker の失敗は画面に出ません。記事が次工程に進まない、週次処理が動かない、在庫が溜まる、という形で静かに残ります。実際に過去には、ある収集工程が長時間止まっていたことに気づけませんでした。

そこで Vector では、非同期パイプラインの各段(dispatch / acquisition / completion / curation / assessment / embedding / backfill / briefing / trend discovery など 11 工程)で起きたことを、append-only の監査ログ `pipeline_events` に記録するようにしました。目的はログを増やすことではなく、**黙って消える失敗を、あとから SQL で追える事実に変える**ことです。設計の第一原理は、「すべてを忘れた未来の読み手が、この 1 行を見て何が起きたかを SQL で再構成できるか」でした。横断で集計したい属性(工程・結果種別・原因コード・retry 可否・記事/ソース ID など)は行の top-level 列に、工程ごとに固有の詳細は payload(JSONB)に置く、という線引きにしています。

この方針は、次の 4 つの判断に分かれます。

- **最新状態ではなく、起きた事実を残す** — 業務テーブルに `failure_reason` のような列を足すだけでは最新状態しか残らず、何度失敗したのか・先週は動いていたのか・どの工程から止まったのかが消えます。そこで各段を「1 行 = 1 イベント」として immutable に記録し、時系列で追える形にしました。dispatch のような起点工程では、ソース単位の結果とは別に「実行そのものが起きた」ことを 1 行残すため、週次処理なら週ごとに錨(anchor)を打ちます。これで「動かなかった週」を、行が無いことではなく事実として観測できます。
- **成功と失敗で、監査の書き込み境界を分ける** — 成功 / skip の監査 INSERT は業務 state 更新と同一トランザクションに置き、「監査行が焼けた = 業務が確定した」を DB レベルで保証します。一方、失敗時は業務 tx が rollback された後に別 session・別 tx で best-effort に焼きます。本文の永続化に失敗したような場合、同じ tx に置くと「永続化に失敗した」という記録まで一緒に rollback で消えてしまう矛盾を避けるためです。書き漏らしゼロは保証せず、監査 INSERT 自体が倒れた経路は警告に留めて業務を優先し、致命的な漏れは Logfire の span で補います。
- **失敗の語彙を、原因・retry 可否・処理方針に分ける** — 「何が起きたか」「同じ入力を再実行して変わりうるか」「記事を消すか」を 1 つの分類 enum に詰めると、直らない例外が retry 中だけ `retryable` と記録され、アラートが最終試行まで黙るような嘘が生まれます。そこで直交した属性に投影し、後から集計しても事実と処理方針が混ざらないようにしました。retry 可否は発生時点で本質的に決まる属性として扱い、「retry 上限に達した」かどうかは本質ではないので別の軸に分けます。原因コードは例外型そのものではなく型が持つ定数から導くので、後で型名を変えても過去の集計 SQL が壊れません。
- **監査を制御状態にしない** — `pipeline_events` は「次に何をするか」を決めるテーブルではなく、発生時点の事実の witness です。再試行 / drop / keep の判断は現在状態や専用の制御テーブルに置き、監査は immutable に保ちます。だから派生的に決まる値や採番後の ID は焼かず、削除に強い識別子だけを残します。記事の物理削除も、provider が「処理対象外」と明示的に拒否したとき(入力がトークン超過や安全性で弾かれた等)に限り、形式不一致のような直り得る失敗では消しません。retry 上限に達した分も即削除はせず、別 cron が一定期間後に消すので、その間にプロンプト改善やモデル切替で過去記事を救える余地を残しています。

この仕組みによって、失敗は「気づけなかった沈黙」から「どの工程で、何が、なぜ起き、記事がどう扱われたかを後から調べられる事実」になりました。壊れないことを保証するためではなく、壊れたときに見つけて直せる状態を作るための設計です。Logfire は補助 telemetry として、span duration・工程別の例外・メモリ逼迫の予兆などを見る役割に分けています。

詳細は [Pipeline Events Design](observability/pipeline-events-design.md)、[Failure Attributes](observability/pipeline-events-failure-attributes.md)、[Error Visibility](observability/error-visibility.md) を参照してください。

## 非同期パイプライン

API サーバーは、保存済みのニュース・分析結果を返す責務に寄せています。ニュース収集、本文抽出、AI 分析、embedding、trend discovery、週次ブリーフィング、maintenance などの重い処理は、API レスポンスの完了条件に含めず、taskiq + Redis Streams の worker pipeline 上で処理しています。外部 fetch や LLM 呼び出しを同期処理に置くと、API の遅延・timeout・外部障害の伝播につながるためです。

pipeline は段階ごとに task / queue を分け、各段階が成功したときに次の task を `.kiq()` で明示的に投入する task chaining にしています。Redis Streams の message を別 worker が検知して次工程を投入する event-driven 方式も取り得ましたが、この pipeline では基本的に次工程が一意に決まるため、routing logic や subscriber を増やさず処理の流れをコード上で追える明示的 chaining を選びました。一時的な失敗や途中で止まった処理は、定期実行される maintenance task が検出し、失敗した段階から再開できるように対象を再投入します。

再投入は放置すると API 使用量や queue 滞留が膨らむため、stage ごとの kill switch・日次予算・hold gate の 3 段で制御しています。日次予算は Redis 上で atomic に更新し、複数 worker が同時に動いても上限を超えません。問題が起きたときは工程単位で止められるため、pipeline 全体を止めずに影響範囲を絞れます。

タスクキューには taskiq を採用しました。これは arq と taskiq の両方を PoC で動かして比較した上での判断です。arq は cron が単一プロセスで完結する利点がありますが、2025-02 以降 maintenance-only に入っていること、リトライに明示的な raise が要る(taskiq は middleware 設定で自動化できる)こと、broker が Redis のみであること、PoC で taskiq の方が大きく高速だったことから、新規採用では避けました。taskiq は cron に別プロセス(scheduler)が必要になりますが、その運用コストは Fly.io の process group で吸収できると判断しています(監視性のため worker と scheduler は別コンテナに分け、1 コンテナへの同居は避けています)。

AI provider は環境変数による切り替えにはせず、composition root で固定配線しています。curation / embedding は Gemini、assessment / 週次ブリーフィングは DeepSeek に固定し、共有 env の設定ミスで工程ごとの provider が入れ替わる事故を構造的に防いでいます。provider を切り替える場合は、コード変更と worker 再起動を必要とする設計です。

## 運用して分かったこと / 取り組み中の課題

デプロイして運用する中で気づいた点と、まだ解けていない課題を残します。

**(解決済み)非 AI worker のメモリ逼迫** — デプロイ後、AI を実行しない scheduler / collect / maintenance のプロセスが、メモリ制限下で落ちることがありました。原因は、各 worker の起動時に AI provider の重い SDK まで一律に読み込んでいたことでした。具体 SDK を各 worker の起動 hook 内で遅延 import し、AI を使う worker にだけ常駐させることで、非 AI プロセスのメモリ使用量を抑えました。

**(取り組み中)成功率メトリクスの分母をどう定義するか** — 工程ごと(curation / assessment / embedding など)の成功率を出し始めていますが、分母の定義を決めきれていません。インフラ起因の失敗(DB / Redis の一時障害、AI provider の quota 超過・タイムアウト)を分母に含めると、ロジックや出力品質ではなくインフラの揺らぎで成功率が上下し、除外すると可用性の問題を見落とします。また 1 記事が複数回リトライされるため、「試行回数」と「記事単位の最終結果」でも値が変わります。`pipeline_events` に原因コード・retry 可否・処理方針を記録しているので、いまは「最終的に retry 不能で終わった記事だけを失敗の分母に入れ、インフラ起因の transient な失敗は別の信頼性指標として分ける」方向を検討しています。

**(取り組み中)アサート失敗・未知例外の工程別可視化** — worker の未知例外や不変条件違反は、現状 Logfire 上で「どの工程で起きたか」を工程軸に絞り込めません。そこで失敗を (1) どの工程で (2) どの場所(ソース / 記事 / 操作)で (3) どんな種別(例外型・原因分類・重大度)だったかの 3 軸で串刺しできるよう、span 計装を段階的に入れています。監査書き込み自体の失敗を集計する counter などは実装済みで、API 側の未知 500 への工程付与は、例外文字列に含まれ得る PII を外部送信前に落とす処理を前提に後段で進めます。

**(取り組み中)AI 呼び出しのバッチ化** — curation / assessment などの AI 工程は、いまは記事を 1 件ずつ呼び出しています。スループットと API コストの観点から複数記事をまとめて処理するバッチ化を検討していますが、1 件の失敗が同一バッチ全体を巻き込まないようにする失敗の局所化と、テーマ3 の工程別失敗分類との両立をどう保つかは未整理です。

**(既知の残リスク)** — 伏せずに記録します。

- migration ゲートの破壊的操作判定に、複文(`SET ...; DROP ...`)を誤って許可しうる fail-open が残っています(follow-up)。
- DB-backed のログイン limiter は best-effort で、DB の部分障害では fail-open しうる残リスクがあります。
- ログイン試行回数を記録するテーブルは TTL による自浄を持たないため、IP ローテーションや分散攻撃で行が増え続け得ます。現状は定期削除を入れず、行数の増加や攻撃トラフィックを観測してから対応する方針です。

## ローカル環境

ローカルは Docker Compose で全 app をまとめて起動し、公開するのは frontend のポートだけにしています。本番の「frontend が唯一の public 入口」という境界を、開発環境でも同じ形で再現しています。

## 検証ゲート(CI)

CI の目的は、失敗を早く・安く見つけ、危険な変更を本番前に止めることです。検証は、走るタイミングの違う三層に分けています。

- **ローカル pre-commit(一次防衛)** — commit 時に手元で、秘密情報の混入(gitleaks)、Dockerfile lint(hadolint)、lint / format(ruff / biome)、基本 hygiene を確認します。速くて頻繁な検査を最前段に置き、大半をここで弾きます。ただし `--no-verify` で迂回できるため、最終防衛ではありません。
- **PR / main push の blocking gate(最終ゲート)** — 品質ゲート(ruff、pytest の unit / integration、biome、tsc、vitest、alembic head 検査)を変更領域ごとに出し分け、全テストを `ci-gate` という単一 job に集約します。並行して、依存ライブラリの既知脆弱性(osv-scanner / npm audit)とソースコードの静的解析(Semgrep, OWASP Top 10)を blocking で検査します。pre-commit で迂回され得る gitleaks / hadolint は、この層で再実行して二重化します。
- **nightly(深い背景検査)** — マージは止めず、毎晩走らせて通知に回す重い検査です。コンテナ・インフラ設定の脆弱性(Trivy)と、OpenAPI 仕様に対する property-based fuzzing(Schemathesis: 未処理の 5xx 露出、宣言外の status code、response schema との不一致)を見ます。

検査ツールは守備範囲が重ならないように選んでいます。gitleaks = 秘密の漏れ、Semgrep = コード自体の脆弱性、osv-scanner / npm audit = 依存ライブラリの既知脆弱性、Trivy = 土台(コンテナ・インフラ設定)、Schemathesis = 実際に叩いて仕様と実装の乖離、という分担です。

新しい検査は、いきなり blocking にすると既存の指摘で全部赤くなり誰も通れません。そこで **warn-only で導入 → 指摘を triage → main が 0 件になったのを確認 → blocking に昇格**、という同じ段階を踏み、昇格後に問題が出たら一時的に warn-only へ戻す退路も許容します。

### 強制力をどう持たせるか

開発当初はプライベートリポジトリかつ GitHub Free で、branch protection / required checks が使えませんでした。そこで代替として全テストを `ci-gate` 1 個に集約し、本番デプロイを `ci-gate` の成功だけに依存させる(個別 job の先走りを 1 ゲートで束ねる)形にしました。スキャン結果も、画面に SARIF を出せないため Actions Artifacts に退避してローカルで triage しています。

その後、有料プランへ移行し、プライベートのままでも branch protection / required checks が使えるようになりました。そこで `ci-gate` をそのまま required status check に指定しています。Free 時代に「代替」として作った集約 job が、無改造で「本物の必須チェック」になり、緑でなければマージできないことを GitHub 側で強制できます。

サプライチェーン面では、GitHub Actions の参照をすべて 40 桁の commit SHA で固定しています。2026 年 3 月に起きた人気 Action のタグ改ざんを踏まえ、可変な tag 参照を禁止し、改ざんに気づかず取り込む経路を塞いでいます。

検査範囲には既知の限界もあります。Schemathesis は現状 GET のみで、変更系や stateful なシナリオは未カバーです。E2E smoke も匿名フローに限られ、認証後の watchlist / search はローカル検証に留めています。

## 関連ドキュメント

- [docs/design-journey/](design-journey/) — 判断に至る経緯と設計思想の変化(ナラティブ)
- [how-i-build-with-ai](how-i-build-with-ai.md) — AI との協働プロセス(red-team レビューを含む)
- [Pipeline Events Design](observability/pipeline-events-design.md) / [Failure Attributes](observability/pipeline-events-failure-attributes.md)
- [Error Visibility](observability/error-visibility.md) / [Memory Monitoring](observability/memory-monitoring.md)
