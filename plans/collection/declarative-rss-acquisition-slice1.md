# 宣言的RSS取得 — スライス1実装プラン

Status: 実装・検証完了
作成日: 2026-09-09
仕様: [宣言的なRSSソースと方式別Fetcherへの移行](../../specs/collection/declarative-rss-acquisition.md)
Issue: [#295](https://github.com/yook11/Vector/issues/295)

## Problem / Evidence

TechCrunch・OpenAIを、ソースに通信手続きや記事全体の写像を書かず、RSS宣言から取得できる形へ移す。既存の取り込み・本文補完の契約は維持する。

確認した現状:

- `ArticleSource[T]`は共通属性と`read / in_scope / select / map_entry`を一つにしている。
- `fetch_articles(source, tools)`が4メソッドを実行し、`ArticleAcquisitionService`が結果を既存converterへ渡す。
- Serviceは`ReaderTools`をfactoryから生成する。新RSS経路の接続に、この依存注入点を利用できる。
- 両ソースは本文を採用せず、title/link/publishedをそのまま写す。TechCrunchは`text`、OpenAIは`bytes`。
- registryテストは旧Protocol・`endpoint_url`・`read`の存在を固定している。写像テストは旧`map_entry()`を直接呼ぶ。
- 一方、source名のlookup、本文補完方針の副作用なしの参照、出力・欠損値・失敗伝達は維持すべき振る舞い。

## Invariants / Non-goals / Done

### Invariants

- 両ソースの取得URL、parse mode、取得頻度、origin、本文補完方針を変えない。
- `FetchedArticle`のtitle/url/published_atを現行どおり渡し、bodyはRSSに本文があっても`None`。
- 空値・不正URL・日時欠落・長いtitleを取得側で削除・品質判定しない。
- 取得エラーを現行の通信失敗・読取失敗として伝達し、実装バグはそのまま伝達する。
- 未移行RSSと非RSSは既存経路で動く。新経路失敗時の旧経路fallbackは設けない。

### Non-goals

- TechCrunch・OpenAI以外のソース移行。
- 本文採用の追加ルール、任意関数、複数フィード巡回の完成（スライス2・3）。
- DB、API、補完・保存・監査・分析の振る舞い変更。
- 全方式のDIやregistry配置の一括再設計。

### Done

両ソースが取得宣言だけで新RSS経路を動き、fixture出力と失敗契約を維持する。既存経路の回帰検証と`/check`が完了し、実装を固定する旧テストのための互換コードが残らない。

## 採用する設計案

### 1. 共通属性と取得契約を分ける

共通属性の契約（`SourceMetadata`）はname、fetch_cadence、observed_origin、completion_policyのみを持つ。converter等、取得を実行しない利用側はこの契約を参照する。

既存`ArticleSource[T]`は未移行の取得契約として残し、新RSS契約は共通属性と`acquisition: RssAcquisition`を持つ。registryは両経路を扱える型で表現する。型のためだけのno-op `read / map_entry`や、メソッド存在確認を通すだけの基底クラスを追加しない。

共通属性は読み取り専用propertyを持つ`SourceMetadata` Protocol、新RSS契約は`RssSource`、入口は`AcquirableSource[T] = ArticleSource[T] | RssSource`で表現する。クラスオブジェクトのProtocol適合は[Python typing公式仕様](https://typing.python.org/en/latest/spec/protocol.html)を参照した。専用のbackend型チェックCLIは未構成であり、静的型チェックの完了は主張しない。

### 2. RSS宣言を不変の値として定義する

- `RssAcquisition`: `feeds: tuple[str, ...]`、`parse_mode`、`body_policy`。
- 空feedsを宣言不備として通信前に拒否する。
- スライス1で実行可能な本文ルールは`DISCARD`。後続の未対応ルールを正常に実行できるようには公開しない。
- 複数URLの表現は維持するが、スライス1で未対応の場合は実行前に明示的に拒否する。先頭だけ取得して成功扱いにしない。複数フィード対応はスライス3で解除する。
- URLのネットワーク安全性は既存Readerを通じて担保し、宣言生成時にDNS解決しない。

### 3. 新ソースのendpoint_urlは持たせない

RSSの取得先は`acquisition.feeds`のみとする。旧`endpoint_url`を要求するテストは削除・置換し、テストのための互換属性は作らない。

既存ソース・DBのendpoint_urlは今回変更しない。取得URLが変わらないことは、宣言を実行したReaderへの入力と出力で確認する。

### 4. RSS Fetcherを既存入口へ接続する

`RssFetcher`は必要なRSS Readerを受け取り、宣言を実行して`FetchedArticle`を出力する。ソースにはReaderや`ReaderTools`を渡さない。

スライス1では共通入口の既存`ReaderTools`注入を利用し、新RSS経路には`tools.rss`だけを渡す。Serviceの保存・監査処理を動かさずに経路を切り替えられる。この段階ではServiceが他方式のReaderも生成する既存構造は残るため、「RSS実行時に他方式の依存が生成されない」とは主張しない。

新宣言の有無・型で明示的に経路を選択する。宣言があるのに不正・未対応なら失敗とし、旧メソッドの呼び出しへ回避しない。ソース名による分岐は禁止する。

## 実装手順と変更対象

### Step 1: 宣言と契約

新規候補:

- `backend/app/collection/sources/source_metadata.py`: 共通属性の契約。
- `backend/app/collection/sources/rss_acquisition.py`: 不変のRSS宣言と本文ルール。

変更候補:

- `sources/article_source.py`: 既存取得契約と共通属性の関係を整理。
- `article_acquisition/strategy.py`: 新旧ソースを登録できる型。
- `article_acquisition/fetched_article_converter.py`: 共通属性の契約へ依存を絞る。

検証: 有効な宣言、空feeds、parse mode・本文ルールの不正値を確認する。宣言を読むだけで通信しない。

### Step 2: RSS取得経路と2ソース移行

新規候補:

- `backend/app/collection/article_acquisition/rss_fetcher.py`: 宣言の実行と標準写像。

変更候補:

- `article_acquisition/fetcher.py`: 新旧経路の明示的な選択。
- `article_acquisition/service.py`: 必要な型注釈の追従に限定し、保存・監査の処理は維持。
- `sources/definitions/techcrunch.py`、`openai.py`: 宣言へ移行しread/map_entry/不要な基底継承を除去。

検証: fake Readerを新入口から動かし、取得URL・parse mode・本文不採用・順序・欠損値・例外伝達を確認する。実Readerの通信安全性・解析テストも既存どおり通す。

### Step 3: テスト整理と既存経路への接続検証

主な対象:

- `tests/collection/article_acquisition/test_strategy.py`
- `tests/collection/article_acquisition/test_fetcher.py`
- `tests/collection/article_acquisition/tools/test_fetched_article_converter.py`
- `tests/collection/sources/test_rss_source_mapping.py`
- `tests/collection/sources/test_rss_adapters_invariants.py`
- `tests/collection/sources/test_source_adapter_profiles.py`
- `tests/collection/sources/test_source_mapping_totality_contract.py`
- `tests/collection/sources/_fixture_tools.py`と共通実行helper（変更が必要な場合のみ）

実装構造を固定するテストは削除可能とする（ユーザー合意）。削除対象に意味のある振る舞い検証が混在する場合は、その部分を新入口へ移す。

| 旧テスト・制約 | 扱い |
| --- | --- |
| 全ソースが旧ArticleSource Protocolを満たす | 削除。新旧が実行できることを確認 |
| 新RSSにもendpoint_urlが存在する | 削除。実際の取得先が維持されることを確認 |
| 全ソースのreadをpatchして非呼出を確認 | 削除または取得側の依存を使った副作用検証へ置換 |
| TechCrunch.map_entryの直接呼び出し | 呼び出し構造の固定を廃止。出力の期待値は新入口から検証 |
| registryのキーとsource名の一致 | 維持 |
| 登録漏れ・ソース別出力・欠損値の通過 | 維持 |
| 補完方針の参照が取得を起動しない | 振る舞いとして維持 |

TechCrunch・OpenAIのfixtureを使い、取得からconverterまでを通して`ObservedArticle`への接続を確認する。Hacker News・Sitemap・HTML一覧・未移行RSSの既存テストも実行する。

## 検証手順

1. 関連の宣言・Fetcher・registry・ソース・converterテストを実行する。
2. `check`スキルのbackend checksを実行する。

```bash
cd backend
uv run ruff check app/
uv run ruff format --check app/
uv run pytest tests/ -x -q
```

3. unit checks通過後、リポジトリrootで`make test-integration`を実行する。
4. 型の整合性は利用可能な既存の型チェック手段を確認して検証する。専用CLIが未構成なら新規依存は追加せず、その制限を報告する。
5. 差分でDB・API・補完ポリシー・分析の振る舞いを変更していないことを確認し、実装結果を仕様へ反映する。

検証結果は末尾に記録する。

## 決めるべきことと推奨

ユーザーの追加判断が必須な業務仕様は現時点ではない。以下の設計判断は上記の推奨案で計画する。

- 旧構造を固定するテストは削除し、新ソースに互換メソッド・属性を足さない。
- スライス1では既存の依存注入点を利用し、RSS FetcherにはRSS Readerだけ渡す。
- 複数フィード・任意関数・本文採用の拡張は後続スライスへ分け、未対応設定を黙って無視しない。

新旧契約の具体的な型表現に問題が見つかった場合は、その実装上の判断を記録する。DBや複数レイヤーの追加再設計が必要になった場合は、今回の計画へ無断で含めない。


## 実装結果（2026-09-09）

- TechCrunch・OpenAIを`RssAcquisition`へ移行した。`read / map_entry / endpoint_url`の互換実装は追加していない。
- 新規ファイルは`source_metadata.py`、`rss_acquisition.py`、`rss_fetcher.py`。RSS Readerの通信処理は変更していない。
- `RssSource`をruntimeで識別し、宣言型を検証する。宣言が不正でも既存経路へfallbackしない。
- 現スライスでは`DISCARD`だけを定義し、複数フィードはFetcherが通信前に拒否する。
- 旧Protocol・endpoint属性・read spyを固定するテストを撤去し、ソース名lookupと補完方針参照の副作用検証を維持した。
- TechCrunch fixtureは正常2件と空titleによる棄却1件、OpenAI fixtureは本文補完待ち3件として後段へ届くことを確認した。
- 新RSS経路は既存`ReaderTools.rss`を利用するため、他方式の依存生成の整理はこのスライスに含まない。

### 検証記録

- RSS取得・ソース関連の単体テスト: 550 passed。
- backend全体の単体テスト: 6065 passed（`-m 'not integration'`）。
- `ruff check app/`、`ruff format --check app/`、変更テストのlint: 通過。
- 最初の関連テスト実行ではDB依存テストも選択され、専用DB未起動により停止したため、単体・統合の選択を分けた。
- 初回の新規fixtureテストは空title行を正常記事と仮定して失敗した。fixtureを確認し、既存どおり棄却理由を検証する期待値へ修正した。
- `make test-integration PYTEST_ARGS='-x -q'`: 1336 passed、22 skipped。一時Postgres・Redisは終了後に削除済み。権限境界テストにはAlembic適用済みschema等を前提とする既存skip条件がある（`tests/test_db_user_isolation.py`）。
- 静的型チェック: 専用CLI・設定がないため未実行。依存追加はしていない。
