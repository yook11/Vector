# 宣言的なRSSソースと方式別Fetcherへの移行

Status: スライス1・2実装・検証完了（スライス3〜4は未実装）
作成日: 2026-09-09
Issue: [#295](https://github.com/yook11/Vector/issues/295)

## Problem

ソースクラスに「取得先・取得条件・固有ルール」を宣言し、方式別Fetcherがその宣言を実行する構造に整理する。最初の対象はRSS・Atom・RDFおよび複数フィード。

移行前は各ソースの`read()`がReaderを選び、引数を組み立てて通信を呼ぶ。`fetch_articles()`は`read → in_scope → select → map_entry`を実行するだけで、ソースが宣言と取得手続きを兼ねている。通常のソースでもReader呼び出しと記事全体の写像を繰り返す必要がある。

目指す追加体験は、通常のRSSソースは設定だけで追加でき、特殊な処理が必要なソースだけ、そのソースクラスに純粋関数を定義すること。取得側が決まったタイミングで関数を呼ぶ。

## Evidence

2026-09-09時点の実装・テストコードの静的確認に基づく。外部サイトの現在の配信内容を保証するものではない。

| 対象 | 根拠 |
| --- | --- |
| ソース契約 | `backend/app/collection/sources/article_source.py`、`base_article_source.py` |
| 実行・登録 | `backend/app/collection/article_acquisition/fetcher.py`、`strategy.py` |
| Readerと依存 | `backend/app/collection/article_acquisition/reader/`、`tools/reader_tools.py` |
| 既存ソース | `backend/app/collection/sources/definitions/` |
| 出口・取り込み | `backend/app/collection/article_acquisition/fetched_article.py`、`fetched_article_converter.py`、`service.py` |
| 合成順序・失敗伝達 | `backend/tests/collection/article_acquisition/test_fetcher.py` |
| ソース別出力 | `backend/tests/collection/sources/test_rss_source_mapping.py`、`test_rss_adapters_invariants.py`、各ソースのfixtureテスト |
| 全ソースの網羅・欠損値 | `backend/tests/collection/sources/test_source_mapping_totality_contract.py` |
| 識別情報・補完方針 | `backend/tests/collection/sources/test_source_adapter_profiles.py` |
| 過去の設計経緯 | `specs/collection/source-completion-profile.md`（旧構造の記述を含む） |

## Invariants

- `FetchedArticle(title: str, url: str, body: str | None, published_at: datetime | None)`の出口契約を維持する。
- 既存の収集対象、取得順、選択・重複除去、本文採用、URL・日時変換の結果を維持する。
- ソース名、取得頻度、観測origin、本文補完方針を維持する。補完方針の参照に通信やReader生成を必要としない。
- 空title・空URL・日時欠落・短い本文を写像で棄却しない。品質判定、URLの安全性検証・正規化は既存の後段へ渡す。
- 通信の安全性、通信失敗と読取失敗の分類・伝達、監査への接続を維持する。
- 任意関数は同期の純粋関数とし、通信・DBアクセス・保存・キュー投入を行わない。
- 任意関数の例外を握りつぶさない。空結果への置換や旧取得経路へのfallbackを行わない。

## Non-goals

- API・Sitemap・HTML一覧ソースの宣言化（後続の対象）。
- 記事完成、本文補完、保存、監査、分析、キュー、Outboxの再設計。
- DBスキーマ、API response、管理画面、認証・認可の変更。
- 新規ソース、新規依存、動的プラグイン、汎用設定言語の追加。
- 取得量・期間・並列度・再試行方針の変更、`parse_mode`の統一。

## 責務と実行境界

| 要素 | 責務 |
| --- | --- |
| ソースクラス | 共通属性とRSS取得設定を宣言し、必要な場合だけ固有関数を定義する |
| RSS Reader | 単一フィードの安全なHTTP取得・解析・`RssEntry`への変換 |
| RSS Fetcher | 宣言に従った巡回、候補の結合、任意関数の適用、本文採用・平文化、`FetchedArticle`への共通写像 |
| 共通取得入口 | ソースの取得契約に対応する経路へ明示的に委譲する |
| 既存取り込みサービス | 出口を受け取り、検証・保存・監査・後段処理へ進める |

巡回機構は既存`MultiFeedRssReader`を移行中に再利用できるが、同じ巡回処理を二重実装しない。最終的な配置はRSS Fetcherの責務に合わせて整理する。

## ソース宣言

`RssAcquisition`と`RssBodyPolicy`は`collection/sources/rss_acquisition.py`に定義する。スライス1では`DISCARD`のみ実装し、他の本文ルールはスライス2で追加する。共通属性は`SourceMetadata`、RSSソース契約は`RssSource`、新旧の入口型は`AcquirableSource`とする。

| 項目 | 必須性・意味 |
| --- | --- |
| `name` | 既存のソース識別名を維持 |
| `acquisition.feeds` | 必須。1件以上のURLを持つ不変の列。列の順番が巡回順 |
| `acquisition.parse_mode` | 明示指定。既存の`text / bytes`をソースごとに維持 |
| `acquisition.body_policy` | 必須。RSSのどの値を本文材料として採用するか |
| `fetch_cadence` | 既存値を維持 |
| `completion_policy` | 既存値を維持。RSS本文の採用ルールとは別の概念 |
| `observed_origin` | 既存の`feed`を維持 |

RSS取得先の正本は`feeds`とする。新RSSソースには旧`endpoint_url`を要求しない。旧属性を要求するテストのための互換コードは作らず、実際の取得先URLが維持されることを検証する。既存ソースの取得先とDBは変更しない。

### 本文採用ルール

| 設計名 | 処理 | 例 |
| --- | --- | --- |
| `DISCARD` | 本文材料を採用せず`None` | TechCrunch |
| `CONTENT_ENCODED` | contentのみを採用 | NASA、Microsoft Research |
| `LONGEST_CONTENT_OR_SUMMARY` | raw文字列の長い方を採用。同長ならcontent | VentureBeat、Meta AI、Frontiers |
| `CONTENT_OR_SUMMARY` | contentが非空なら採用し、なければsummary | PLOS ONE |

採用後に既存と同じ「タグを空白に置換 → entity decode → 空白圧縮 → 前後空白除去」を行い、その後に任意の`transform_body`を適用する。段落改行を残す汎用整形関数には置き換えない。最終的に空文字なら`None`にする。長さ比較を平文化後へ移さない。採用後に空になっても別候補を採り直さず、本文変換があれば空文字も渡す。`DISCARD`では本文後処理を呼ばない。

### 任意関数

ソースクラスに必要な関数を`staticmethod`として定義し、取得側が`runtime_checkable`なProtocolへの適合を`isinstance`で判定して呼ぶ。未定義なら標準動作を使う。ソースはProtocolを継承しない。特殊ソースのフラグ、ソース名による分岐、関数の登録辞書、独自の署名検証は作らない。

スライス2の契約は`sources/rss_hooks.py`の`RequiresBodyTransform`・`RequiresUrlTransform`・`RequiresPublishedAtResolution`・`RequiresScopeFilter`。`select`の契約・実行はスライス3で追加する。

| 関数 | 入出力 | 未定義時 |
| --- | --- | --- |
| `in_scope` | `RssEntry → bool` | 全候補を対象とする |
| `select` | `list[RssEntry] → list[RssEntry]` | 順番・件数を変えない |
| `transform_url` | `str → str` | `entry.link`をそのまま使う |
| `resolve_published_at` | `RssEntry → datetime \| None` | `entry.published`を使う |
| `transform_body` | `str → str` | 平文化済み本文をそのまま使う |

`resolve_published_at`は日時の解決全体を担当する。FierceBiotechでは既存の解析済み日時を優先し、欠落時だけraw日時を独自解析する。`None`を返した場合に取得側が別の日時を捏造しない。

通常の写像はtitle=`entry.title`、URL=`entry.link`、日時=`entry.published`。ソースに記事全体の`map_entry()`や通信する`read()`を書かせない。任意関数は入力候補を破壊的に変更しない。

### 宣言例（設計例）

```python
class MicrosoftResearchSource:
    name = SourceName("Microsoft Research")
    acquisition = RssAcquisition(
        feeds=("https://www.microsoft.com/en-us/research/feed/",),
        parse_mode="text",
        body_policy=RssBodyPolicy.CONTENT_ENCODED,
    )
    fetch_cadence = FetchCadence.MEDIUM
    completion_policy = DEFAULT_POLICY
    observed_origin = ObservedOrigin.feed

    @staticmethod
    def transform_body(body: str) -> str:
        return strip_footer(body)
```

## 実行順序と失敗契約

以下は複数フィード対応後も含む最終契約。スライス2では単一フィードのみを取得し、`select`を実行せず、scope選別 → 候補ごとのURL変換 → 日時解決 → 本文採用・平文化・本文変換の順とする。`RssFetcher.fetch(source: RssSource)`がソースを受け取り、Reader注入と共通入口の宣言検証を維持する。

1. `feeds`を宣言順に取得・解析し、各フィード内の順序を維持して結合する。
2. `in_scope`で対象候補を選ぶ。
3. `select`を候補列に適用する。
4. 候補ごとにURL変換、日時解決、本文採用・平文化・本文後処理を行う。
5. 共通写像で`FetchedArticle`を出力する。

- 空の`feeds`は宣言不備として実行前に検出する。
- 複数フィードは既存どおり逐次取得。一部の通信・読取失敗は記録して続行する。
- 1フィードでも成功したらその候補を使用する。正常に読めた空フィードも成功に含む。
- 全フィード失敗時は最初の通信・読取エラーを伝播する。
- 関数や解析実装の想定外の例外は部分的な通信失敗に変換しない。
- 固有関数の例外はFetcherで捕捉・再分類せず、元の例外・原因を既存のソース取得失敗処理へ渡す。標準処理へのfallbackや記事単位の握りつぶしは行わない。後続候補で失敗した場合も既存の保存トランザクションを維持し、未commit分をrollbackする。
- NASA・Cornellの重複除去は非空のraw linkをキーに初出を残す。URL変換より前に実行し、空linkはすべて後段へ渡す。

## 移行中の契約

共通属性の参照契約と、RSSの宣言実行契約を分ける。従来の`ArticleSource`が要求する`read / map_entry`を満たすためだけに、新RSSソースに同じメソッドを再導入しない。

共通入口ではRSS宣言のある新経路と非RSSの既存経路を明示的に接続する。移行中のRSS旧経路も未移行分のみ残せる。新経路の失敗を受けて旧経路を試すfallbackは禁止する。

既存の`ReaderTools`は非RSS経路で必要な間は維持する。新RSSソースに全方式の道具箱を渡さず、RSS取得側に必要な依存を注入する。未移行ソースのための仕組みを汎用プラグイン基盤へ拡張しない。

## 実装スライス

### 1. 宣言から取得する最小経路

詳細: [スライス1実装プラン](../../plans/collection/declarative-rss-acquisition-slice1.md)

- RSS宣言型、共通属性の契約、RSS Fetcherと既存取り込みへの接続を実装する。
- TechCrunch・OpenAIを移行する。
- 宣言検証、取得先の正本、Readerの依存注入、明示的な経路選択を確定する。

完了条件: ソースに通信・記事全体の写像を書かず取得でき、本文不採用・`text / bytes`・空値・失敗伝達を維持する。非RSSの既存経路が動く。

### 2. 本文採用と固有関数

詳細: [スライス2実装プラン](../../plans/collection/declarative-rss-acquisition-slice2.md)

- 残る本文採用ルールと任意関数の呼び出しを実装する。
- VentureBeat・PLOS ONE・Microsoft Research・The Register・FierceBiotech・Meta AIを移行する。

完了条件: 本文選択、フッター除去、URL変換、日時補完、対象判定を再現する。関数未定義時、適用順序、例外伝播を検証する。

### 3. 複数フィード

- NASA・Cornellを移行する。
- 巡回・結合・部分失敗・`select`の適用を共通化する。

完了条件: 宣言順、初出維持、空linkの通過、部分成功・全失敗・空フィード成功を再現する。

### 4. RSS全体の移行と整理

- 残るRSS・Atom・RDFソースを移行する。
- 不要になったRSS用の`read / map_entry`、薄い委譲関数、重複した本文処理を削除する。
- 全ソースの網羅確認と追加手順を記録する。

完了条件: 全RSS系ソースが宣言と任意関数のみになり、RSSの旧経路が残らない。非RSS経路・既存の出口契約を維持する。

## 検証とDone

各スライスで関連テストと`/check`を実施する。ユーザー合意により、旧実装の構造だけを固定しリファクタリングを妨げるテストは削除してよい。振る舞いの保証が混在する場合はその部分を新入口へ移し、出力・欠損値・失敗の期待値を実装に合わせて弱めない。

- [ ] 4スライスの完了条件を満たす。
- [ ] 共通契約テストで任意関数の有無、適用順序、例外伝播を確認する。
- [ ] ソース別fixtureで本文・URL・日時・対象選別・重複除去の同等性を確認する。
- [ ] RSS系の登録ソースと検証対象の集合が一致する。
- [ ] 取得頻度・origin・補完方針の参照に通信や取得機構の生成を伴わない。
- [ ] 非RSS経路の回帰を確認する。
- [ ] 通常のRSS追加と固有関数を持つRSS追加の手順を記録する。
- [ ] 検証結果と未実行項目・理由を記録する。

各スライスの実装・検証結果はリンク先の実装プランに記録する。上記Doneは全4スライスの完了条件であり、スライス2まででは完了としない。非RSSへの展開は別の実装計画とする。

### スライス2の検証結果（2026-09-09）

- 6ソースを宣言と固有関数へ移行し、旧実装と7fixture・17記事の全フィールドが一致した。
- 本文・関数の契約、VentureBeatの分析可能／補完待ちへの分岐、後続候補失敗時のrollbackと監査への原因伝達を確認した。
- backend lint・format通過、単体 **6101 passed**、専用DB統合 **1338 passed / 22 skipped**。
- skipは既存のDB権限境界テストがAlembic適用済み`public.watchlist_entries`を要求するため。専用backend型チェックCLIは未構成のため静的型チェック未実行。詳細はスライス2実装プランに記録した。
