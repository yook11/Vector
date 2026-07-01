# Architecture

Vector は、海外テックニュースを自動収集し、AI で翻訳・要約・分析して投資判断を助けるダッシュボードです。個人で開発・運用している本番アプリケーションで、コードの多くは AI との協働で書いています。

本書は「各構成をなぜそう決めたのか」を、検討した代替案と受け入れたトレードオフとともに記録する、**設計判断の正本**です。判断に至る経緯や設計思想の変化は [docs/design-journey/](design-journey/) に、AI とどう協働して作っているかは [how-i-build-with-ai](how-i-build-with-ai.md) に分けています。


## 設計前提

Vector は個人で開発・運用している本番アプリケーションです。運用に使える時間・費用・CI の月間実行枠には上限があるため、設計判断では「本番運用上のリスクをどこまで下げるか」と「個人で継続的に運用できるか」のバランスを重視しています。

GitHub Actions は月間実行枠を使い切ることがあり、すべての変更で CI を完走させてからマージできていたわけではありません。現在は `ci-gate` を required check として運用していますが、過去の履歴には自動検証が十分に完了していない状態で取り込んだ変更も含まれます。


## このプロジェクトで重点的に取り組んだこと

Vector では、機能を作るだけでなく、個人で本番運用するアプリとして「どこが危ないか」「どこで壊れやすいか」「後から原因を追えるか」を意識して設計しました。特に次の 3 点に重点的に取り組んでいます。

1. **脅威を前提にしたセキュリティ設計**
   Claude Code で red-team レビュー用のコマンドを作り、外部 HTML 取得、認証境界、DB / Redis 権限、secret の分離、公開経路などのリスクを洗い出しました。セキュリティに十分詳しいとはまだ言えず、完璧な対策ができているとも考えていませんが、攻撃者目線で自分の設計を見ることで、app 分割、最小権限、内部ネットワーク化、BFF 経由の認証境界など、多くの改善につながりました。

2. **不正な状態を作りにくくする設計**
   ニュース収集から AI 分析までの非同期パイプラインでは、「この記事は次工程に進める状態か」「この値は保存してよい形式か」といった前提条件が多くあります。そこで、各処理で毎回 `if` を増やすのではなく、Ready 型、値オブジェクト、DB 制約、責務ごとのテーブル分離によって、処理できる状態だけを次の層へ渡す設計を目指しました。

   これは型安全そのものが目的ではなく、バグが入りやすい確認処理を散らさず、壊れにくく変更しやすい構造にするための取り組みです。

3. **失敗を後から追える運用基盤づくり**
   worker の失敗は画面に出にくく、気づかないまま記事が次工程に進まないことがあります。そのため、各工程の成功・失敗・skip を `pipeline_events` に残し、工程、原因、retry 可否、処理方針を後から SQL で追えるようにしました。

   一方で、これを日常的な alert、runbook、復旧判断に十分つなげられているとはまだ言えません。
   今後は工程別の SLO 的な指標、失敗率の分母定義、手動復旧 SQL / requeue 手順、アラート条件を整備していく段階です。


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


## テーマ 1: セキュリティで意識した部分

Vector で最も危ない入口は、外部サイトの HTML を取得して解析する収集処理です。中身を自分で制御できない入力を扱うため、RCE / SSRF の起点になり得ます。

そこで Claude Code で red-team レビュー用のコマンドを作り、本リポジトリに対して敵対的な観点から設計を見直しました。セキュリティに十分詳しいとはまだ言えず、完璧な対策ができているとも考えていません。ただ、攻撃者目線で見ることで、外部入力を扱う層をどこで隔離すべきか、どの secret や権限を分けるべきかを学ぶ大きな機会になりました。

この章では、実害を観測した対策ではなく、脅威を想定して入れた予防的な設計判断をまとめます。基本方針は、**どこかが破られても、その影響をできるだけ狭い範囲に閉じ込めること**です。

### 侵害時の被害を閉じ込める

ブラウザから到達できるのは `vector-frontend` だけで、backend 以降は Fly の内部ネットワークに閉じています。外部 HTML を扱う `vector-collect` は、AI provider の API key や BFF の署名鍵を持つ `vector-core` から app ごと分離し、両者は `vector-redis` のキュー越しにのみつながります。

この分離を app だけでなく、DB role、Redis、secret、通信経路にも重ねています。

| 対象 | 採った対策 | 狙い |
|----|----|----|
| Fly app | `core` と `collect` を別 app に分離 | collect が破られても、AI key・BFF 署名鍵・分析結果へ直接届かないようにする |
| DB role | app / auth / collect / migration owner の権限を分離 | 侵害された app が触れる table と操作を限定する |
| Redis | collect 専用ユーザーを作り、必要な queue 操作だけを許可 | collect 侵害で Redis 全体、他工程の queue、AI 予算、rate limit を操作されないようにする |
| Secret | 用途ごとに secret を分け、弱い値や同一値は起動時に拒否 | 軽い権限の漏洩が、署名鍵の漏洩に波及しないようにする |
| Transport | backend の公開アドレスを廃止し、内部ネットワークからのみ到達可能にする | JWT 検証ミスや署名鍵漏洩だけでは、backend を直接叩けないようにする |

単一 app、単一 DB role、単一 Redis password に寄せると、外部入力を扱う collect の侵害がそのまま重要な secret や DB 全体へ広がります。そのため、危険な処理と守りたい資産を同じ場所に置かず、各層で最小権限を重ねる設計にしました。

本番では、backend の接続先が内部ネットワークの住所でなければ起動を止めます。開発用の `localhost` などを誤って設定しても、こっそり動かず起動失敗で気づけるようにしています。

### 認証境界を frontend に寄せる

認証まわりは、最初から現在の形だったわけではありません。当初は backend が login / refresh token / JWT 発行を持ち、FastAPI 側に認証 router と auth service、User / RefreshToken model を置いていました。

その後 Better Auth を採用するタイミングで、認証 session の検証を frontend(BFF) 側に寄せました。frontend が Better Auth の httpOnly Cookie session を検証し、backend には認証済みユーザーの情報だけを内部リクエストとして渡す構成です。これにより、backend から認証 router や refresh token 管理を外し、API サーバーをアプリケーションデータの提供に寄せられました。

ただし、最初の BFF 実装は `X-User-ID` / `X-User-Role` / `X-Internal-Secret` を backend に渡す方式でした。この形は実装としては単純でしたが、内部 secret が漏れた場合に `X-User-Role: admin` のようなヘッダー偽装を許し得る弱点がありました。

そこで後から、BFF が Better Auth session から user_id / role を取り出し、短期 JWT に署名して backend へ渡す方式に変えました。backend は JWT の署名、有効期限、issuer、audience を検証し、正しい BFF から来たリクエストだけを信じます。

この認証境界は、最初から完成形として設計できていたものではありません。backend 認証を単純化するために BFF へ寄せ、その後のセキュリティレビューで見つかった弱点を、短期 JWT・claim 検証・secret 分離で段階的に固めていった部分です。

ログイン試行制限も、後から見直した部分です。以前は失敗回数を Redis に置いていましたが、Redis 障害時に制限が外れるリスクがありました。現在は Better Auth の limiter を DB-backed にし、rate limit 用 Redis が落ちてもログイン試行制限が無制限には開かないようにしています。

ただし、この DB-backed limiter は厳密なアトミック加算ではないため、高並行下では取りこぼし得ます。また `auth.rateLimit` テーブルには TTL による自動削除がないため、IP ローテーションや分散攻撃で行が増え続ける可能性があります。現時点では best-effort な brute-force 対策として扱い、行数や攻撃トラフィックを見て prune や CAPTCHA / account lockout を検討します。

### 止める失敗と通す失敗を分ける

rate limit も、最初から今の粒度で設計できていたわけではありません。初期の frontend proxy では、静的アセット以外の request を単一の IP 制限で数えていました。その結果、Next.js の RSC / prefetch / API request が同じ枠を消費し、通常の画面遷移でも 429 が返ることがありました。

ここで単純に上限値を上げると、通常操作は通りやすくなりますが、ログイン試行や変更系 request まで一緒に緩くなります。問題は上限値そのものより、性質の違う request を同じ枠で数えていたことでした。

そこで、prefetch / 読み取り / 変更系を分け、認証済み request は session と IP の両方で見る形に変えました。通常操作で増えやすい prefetch は専用枠で寛容に扱いつつ、完全には除外しないようにしています。認証済み Cookie を使った RSC 連打を無制限に通すと、session 検証が DB 負荷につながるためです。

fail-open / fail-closed の扱いも、後から整理しました。通常閲覧の rate limit Redis が落ちた場合に全 request を止めると、rate limit の障害がそのままサービス停止になります。そのため、閲覧系は Redis 障害時に fail-open します。一方で、信頼できる IP 情報が本番で取れない、弱い secret が設定されている、内部 URL が production で flycast ではない、といった状態は、動かすこと自体が危険なので fail-closed にしています。

この部分も、最初からきれいに分類できていたわけではありません。通常操作を止めてしまう rate limit、Redis 障害時に開きすぎるログイン制限、危険な設定でも起動できてしまう問題を見つけるたびに、「止めるべき失敗」と「通した方がよい失敗」を分け直してきました。


## テーマ 2: 不正状態を作りにくくする構造

Vector の非同期パイプラインでは、外部ソースから取れた記事、本文取得後の記事、AI 分析に進める記事、分析済みの記事など、工程ごとに満たすべき条件が変わります。これを一つの「記事」モデルに寄せると nullable な値や状態判定が増え、Service や Task の各所で「この値は本当に使ってよいか」を確認し続ける必要があります。

そこで、前提条件を呼び出し側の注意に任せず、型・DB 制約・テーブル境界に寄せる設計にしています。目的は型を増やすことではなく、処理を始めてよい条件をどこで確定させるかを明確にすることです。

具体的には、次のように役割を分けています。

- `ObservedArticle` / `AnalyzableArticle` などで、外部から取れた事実と分析に進める記事を分ける
- `ReadyForCuration` / `ReadyForAssessment` / `ReadyForEmbedding` などで、各工程を開始できる状態を表す
- `CategorySlug` や URL 系の値オブジェクトで、アプリケーション内で意味を持つ値の形式を閉じ込める
- DB の `CHECK` 制約や composite FK で、保存されるデータの最後の整合性を守る

`ReadyFor...` 型は、前工程から渡された「次に進めるはず」という合図ではありません。各工程が開始時点で DB の現在状態を読み直し、対象が存在し、未処理で、必要な値が揃っているときだけ構築します。Service は `Ready` を受け取った時点で、キュー状態や claim 状態を毎回確認せず、処理本体に集中できます。

値オブジェクトも同じ考え方です。たとえば `CategorySlug` は「カテゴリの URL 識別子として使える文字列」であることを表します。ただし、型に何でも保証させるわけではありません。URL の形式として正しいことと、実際にサーバーが通信して安全な相手かどうかは別の問題です。型が保証する範囲を決め、それを超える条件は別の境界で扱います。

この設計を ORM 層にも保つため、モデル層は SQLModel から SQLAlchemy 2.0 の DeclarativeBase へ移行しました。`Mapped[CategorySlug]` のように DB model 上でも値オブジェクトの型を残し、`TypeDecorator` と `type_annotation_map` で DB 読み書き時の変換を一箇所に寄せています。

このテーマで重視しているのは、抽象を増やすことではなく、確認が必要な場所を減らすことです。すべてを型だけで守るのではなく、処理開始時点の保証は `ReadyFor...`、値の形式は値オブジェクト、永続化後の整合性は DB 制約、というように、保証する責任を分けています。

判断に至る過程は [第2幕: 値オブジェクト](design-journey/02-value-objects.md) と [第6幕: ドメインモデル再構築](design-journey/06-domain-model-rebuild.md) に残しています。

## テーマ 3: 黙って消える失敗を見える形にする

HTTP リクエストの失敗は、ユーザーに 500 として返ります。一方で worker の失敗は画面に出ません。記事が次工程に進まない、週次処理が動かない、queue に在庫が溜まる、といった形で静かに残ります。実際に過去には、収集工程が長時間止まっていたことに気づけなかったことがありました。

そこで Vector では、非同期パイプラインの各工程で起きたことを `pipeline_events` に記録し、Logfire の span / metric でも工程ごとの失敗や成功率を見られるようにしています。目的は、障害対応を自動化できているということではなく、まず失敗が黙って消えず、あとから原因を追える材料を残すことです。

`pipeline_events` は、現在の状態ではなく「起きた事実」を残す append-only の監査ログです。業務テーブルに最新の `failure_reason` だけを持たせても、何度失敗したのか、どの工程から止まったのか、いつから起きていたのかは追えません。そのため、各工程の成功・失敗・skip を 1 行 = 1 イベントとして記録し、工程、結果種別、原因コード、retry 可否、処理方針、記事 / ソース ID などを SQL で追えるようにしています。横断で集計したい属性は top-level 列に置き、工程固有の詳細は payload(JSONB) に分けています。

監査ログの書き込み境界も、成功と失敗で分けています。成功 / skip は業務更新と同じ transaction に置き、「監査行があるなら業務状態も確定している」と見なせるようにしています。一方で失敗時は、業務 transaction が rollback されたあとに別 transaction で best-effort に記録します。本文保存に失敗した記録まで同じ rollback に巻き込まれて消えることを避けるためです。

失敗の表し方も、あとから集計できるように分けています。原因、retry 可否、処理方針を一つの分類に押し込むと、「何が起きたか」と「その失敗をどう扱うか」が混ざります。そこで、原因コード、retry 可否、drop / keep などの扱いを別々の属性として記録し、後から工程別・原因別に見られるようにしています。あわせて Logfire には工程別の span や processing outcome metric を追加し、どの工程で失敗が増えているかを見られるようにしています。

一方で、この仕組みを日常的な alert、runbook、復旧判断に十分つなげられているとはまだ言えません。監査ログとメトリクスは入れ始めていますが、障害時に「どの SQL を見て、どの条件なら requeue し、どの条件なら記事を drop するか」まで運用手順として固まっているわけではありません。

今後は、工程別の SLO 的な指標、失敗率の分母定義、手動復旧 SQL / requeue 手順、アラート条件を整備していく段階です。このテーマは、完成した incident response ではなく、worker の失敗を見つけ、調べ、復旧手順につなげるための土台づくりとして位置づけています。


## 非同期パイプライン

ニュース収集、本文抽出、AI 分析、embedding、trend discovery、週次ブリーフィングのような重い処理は、API レスポンスの完了条件に含めず、taskiq + Redis Streams の worker pipeline で処理しています。外部 fetch や LLM 呼び出しを同期 API に置くと、API の遅延、timeout、外部サービス障害の影響がそのまま画面に出るためです。

API サーバーは、保存済みのニュースや分析結果を返す責務に寄せ、重い処理は worker に逃がしています。

| stage | 役割 |
|----|----|
| dispatch | 対象ニュースソースを見つけ、収集 task を投入する |
| acquisition | RSS / sitemap / API などから記事候補を取得する |
| completion | 本文や公開日時が足りない記事を HTML から補完する |
| curation | AI で記事を投資分析対象にするか振り分ける |
| assessment | 投資家向けの要約・示唆・分類を生成する |
| embedding | 検索や関連記事に使うベクトルを作成する |
| trend discovery | 一定期間の記事から注目トレンドを抽出する |
| briefing | 週次ブリーフィングを生成する |
| maintenance / backfill | 途中で止まった処理や再処理対象を検出して戻す |

queue には Redis Streams を使っています。pipeline は stage ごとに task / queue を分け、各 stage が成功したときに次の task を `.kiq()` で明示的に投入します。

Redis Streams の message を別 worker が購読し、次工程を自動投入する event-driven な構成も考えました。ただ、この pipeline では多くの工程で次に進む先がほぼ一意です。そのため、subscriber や routing logic を増やすより、成功した task が次の task を直接投入する形の方が、処理の流れをコード上で追いやすいと判断しました。

一方で、task chaining だけにすると、worker の途中停止や一時的な失敗で処理が止まったまま残る可能性があります。そこで、定期実行される maintenance task を置き、途中で止まった対象や再実行可能な失敗を検出して、必要な stage から再投入します。通常の流れは `.kiq()` による明示的な chaining で進め、取りこぼしの救済は cron 的な maintenance に任せる分担です。

再投入は放置すると API 使用量や queue 滞留が膨らむため、stage ごとの kill switch、日次予算、hold gate で制御しています。問題が起きたときは pipeline 全体を止めるのではなく、影響のある stage だけを止められるようにしています。


## 検証ゲート(CI)

セキュリティ対策や品質確認は、人間の注意だけに任せると抜け漏れが起きます。Vector では、学びながらではありますが、秘密情報の混入、lint / format、型チェック、テスト、依存ライブラリの脆弱性、基本的な静的解析を CI に組み込み、自動で検出できる範囲を増やしてきました。

検証は、軽いものから重いものへ段階を分けています。手元では pre-commit で gitleaks、hadolint、ruff、biome などを走らせ、PR / main push では pytest、tsc、vitest、alembic head 検査、osv-scanner、npm audit、Semgrep などを実行します。さらに nightly では、Trivy や Schemathesis のような重めの検査を回し、マージを止めない背景検査として扱っています。

この構成は、最初から完璧に理解して設計したものではありません。セキュリティレビューやリサーチを通じて、gitleaks は秘密情報、Semgrep はコード上の危険なパターン、osv-scanner / npm audit は依存関係、Trivy はコンテナや設定、Schemathesis は OpenAPI と実装のずれを見る、という役割分担を学びながら組み込んでいます。

新しい検査はいきなり blocking にせず、まず warn-only で導入し、指摘を確認してから blocking に昇格する方針にしています。既存の指摘で全ての変更が止まると、検査自体を維持できなくなるためです。

また、個人開発では GitHub Actions の月間実行枠にも制約があります。すべての履歴で CI を完走させてからマージできていたわけではありませんが、現在は品質ゲートを `ci-gate` に集約し、required check として運用しています。実行枠を使い切った場合は、検証を再開できるまで待つ必要があります。

まだ検査範囲には限界があります。Schemathesis は主に GET の検証に寄っており、変更系や stateful なシナリオは十分ではありません。E2E も匿名フローが中心で、認証後の watchlist / search などはローカル検証に残っています。この章は「CI が完成している」というより、セキュリティや品質の確認を少しずつ自動化に寄せている取り組みとして位置づけています。

## 運用して分かったこと / 取り組み中の課題

実際にデプロイしてみると、ローカル開発では見えていなかった問題がいくつも出ました。コードとして正しく動くことと、ユーザーが触ったときに安心して使えること、本番環境の制約の中で安定して動くことは別の問題だと学びました。

**非 AI worker のメモリ逼迫** — デプロイ後、AI を実行しない scheduler / collect / maintenance のプロセスが、起動時点でメモリ制限に当たることがありました。原因は、各 worker が起動時に AI provider の重い SDK まで一律に読み込んでいたことでした。現在は SDK を必要な worker の起動 hook 内で遅延 import し、非 AI プロセスの常駐メモリを抑えています。

**フロントエンドの使用感** — フロントエンドは詳しくない状態から作り始めましたが、実際に触る中で、ロード中か分からない、ボタンを押せたのか分からない、サーバーのコールドスタートで timeout する、といった問題に気づきました。API が正しいレスポンスを返すだけでは不十分で、待ち時間や失敗状態を画面でどう見せるかも運用上の品質だと感じています。

**外部サービスや無料枠の制約** — DB や CI、AI provider などには実行枠や課金上限があり、使い切ると処理が止まることがあります。止まったあとに気づくのでは遅いため、残量や失敗を可視化し、どこで止まっているかを見られる仕組みが必要だと分かりました。監査ログやメトリクスは入れ始めていますが、日常的な alert や復旧手順としてはまだ整備途中です。

**安全なデプロイ手順** — 新しい実装に置き換えるとき、古いコードをすぐ消すと、本番で問題が出たときに戻しにくくなります。新旧のコードを一時的に共存させ、デプロイ後に動作確認してから古い経路を削除する進め方が必要だと学びました。まだ十分に体系化できてはいませんが、変更を小さく出し、確認してから片付ける意識を持つようになりました。

## 関連ドキュメント

- [docs/design-journey/](design-journey/) — 判断に至る経緯と設計思想の変化
- [how-i-build-with-ai](how-i-build-with-ai.md) — AI との協働プロセス
- [Pipeline Events Design](observability/pipeline-events-design.md) / [Failure Attributes](observability/pipeline-events-failure-attributes.md)
- [Error Visibility](observability/error-visibility.md) / [Memory Monitoring](observability/memory-monitoring.md)
