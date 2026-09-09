# 宣言的RSS取得 — スライス2実装プラン

Status: 実装・検証完了
作成日: 2026-09-09
仕様: [宣言的なRSSソースと方式別Fetcherへの移行](../../specs/collection/declarative-rss-acquisition.md)
Issue: [#295](https://github.com/yook11/Vector/issues/295)

## Problem / Evidence

6ソースに重複するRSS取得・本文採用・記事写像を共通化し、ソースには取得宣言と必要な固有関数だけを残す。

- スライス1の`RssSource`・`RssAcquisition`と`fetch_articles`による新旧経路の切り替えを利用する。
- 6ソースの旧`read / map_entry`、既存fixture、ソース別テストから取得先・parse mode・本文と日時の扱いを確認した。
- `ArticleAcquisitionService.execute`は候補を逐次保存し、全件取得後にcommitする。取得途中の例外では未commit分がrollbackされる。
- `acquire_source`と既存失敗handlerは元の例外を監査へ渡す。想定外例外の監査には例外クラス・メッセージ・原因チェーンが残る。

## Invariants / Non-goals / Done

取得URL、parse mode、取得頻度、origin、補完方針、`FetchedArticle`の出力を維持する。本文の品質判定や分析・補完への分岐は既存converterに任せる。任意関数は同期の純粋関数であり、例外をFetcherで捕捉・再分類せず、標準処理へのfallbackや記事単位の握りつぶしをしない。

複数フィード・`select`はスライス3の対象。DB・API・依存パッケージ・再試行方針・保存トランザクションは変更しない。追加の選択事項はない。

6ソースが宣言と必要な固有関数へ移行し、出力・後段接続・失敗時のrollbackと監査を検証できれば完了とする。仕様・本プランへ検証結果を記録し、一つのPRにまとめる。

## 契約

### 任意関数の検出

`sources/rss_hooks.py`に`runtime_checkable`なProtocolを置き、Fetcherが`isinstance`で判定する。ソースはProtocolを継承せず、必要な関数を`staticmethod`として定義する。Protocolは呼び出し契約であり、独自の署名検証は追加しない。

| Protocol | 関数 | 未定義時 |
| --- | --- | --- |
| `RequiresBodyTransform` | `transform_body(body: str) → str` | 平文化した本文 |
| `RequiresUrlTransform` | `transform_url(url: str) → str` | `entry.link` |
| `RequiresPublishedAtResolution` | `resolve_published_at(entry: RssEntry) → datetime \| None` | `entry.published` |
| `RequiresScopeFilter` | `in_scope(entry: RssEntry) → bool` | 全候補 |

特殊ソースのフラグ、ソース名による分岐、関数の登録辞書は作らない。

### 本文採用と実行順序

`RssFetcher.fetch(source: RssSource)`はReader注入を維持し、宣言に従う単一フィード取得 → 全候補のscope選別 → 候補ごとのURL変換 → 日時解決 → 本文採用・平文化・本文変換 → `FetchedArticle`出力の順に実行する。共通入口の宣言検証も維持する。

| 本文ルール | 採用方法 |
| --- | --- |
| `DISCARD` | `None`。本文変換を呼ばない |
| `CONTENT_ENCODED` | contentのみ |
| `LONGEST_CONTENT_OR_SUMMARY` | raw文字列の長い方。同長ならcontent |
| `CONTENT_OR_SUMMARY` | contentが非空ならcontent、それ以外はsummary |

平文化は「タグを空白に置換 → entity decode → 空白圧縮 → 前後空白除去」。段落改行を残す汎用整形関数とは分ける。採用後に空文字になっても他候補を採り直さず、本文変換があれば空文字も渡す。変換後の空文字を`None`にする。

### 移行順序

| 順序 | ソース | 宣言・固有処理 |
| --- | --- | --- |
| 1 | VentureBeat | 長い方の本文 |
| 1 | PLOS ONE | content優先、欠落時summary |
| 2 | Microsoft Research | contentのみ＋本文フッター除去 |
| 3 | The Register | 本文不採用＋redirector URL展開 |
| 3 | FierceBiotech | 本文不採用＋既存日時優先・raw日時の米国東部時間からUTCへの解釈 |
| 4 | Meta AI | 長い方の本文＋大文字小文字を区別したAIタグ判定 |

## 実装と検証

1. `rss_acquisition.py`へ本文ルールを追加し、`rss_hooks.py`へ任意関数の契約を追加する。
2. `rss_fetcher.py`と共通入口を更新する。例外処理・保存処理は変更しない。
3. 上表の6ソースから`read / map_entry`・重複する平文化処理・不要な基底クラス継承を除去する。
4. 旧`map_entry`の直接呼び出しテストは共通入口へ移す。空値・不正URL・長い本文・日時欠落などの期待値を維持する。
5. 本文の同長・欠落・空文字・rawと平文化後の長さ逆転、関数未定義・順序・併用・元の例外と原因を検証する。
6. 既存fixtureで6ソースとTechCrunch・OpenAI・未移行経路を検証する。VentureBeatの全文は分析可能、短い概要は補完待ちになることを確認する。
7. 実DBと実Service・Task・監査処理を使い、先行候補の保存後に後続候補の本文変換を失敗させる。分析可能記事・補完待ち記事の両方で、記事・Outbox・作成監査のrollbackと失敗監査への原因伝達を確認する。
8. `check`スキルのbackend lint・format・単体・専用DB統合テストを実行する。

単体は`uv run pytest tests/ -m 'not integration' -x -q`、統合はrootで`make test-integration PYTEST_ARGS='-x -q -rs'`。専用backend型チェックCLIは未構成のため新規依存を追加せず、静的型チェック未実行を制限として明記する。

## 実装結果

- 6ソースを移行し、通信・記事全体の写像・平文化の重複を除去した。
- 既存のReader・converter・Service・失敗handler・監査・DBモデルは変更していない。
- Meta AIの既存公開scope predicateは維持し、`in_scope`から呼び出す。
- 複数フィードの宣言は引き続き通信前に拒否し、`select`の追加は行っていない。

### 検証記録（2026-09-09）

- `ruff check app/`、`ruff format --check app/`と変更テストのlint・format: 通過。
- backend単体テスト: **6101 passed**（`-m 'not integration'`）。本文ルール・任意関数・後段接続と、TechCrunch・OpenAI・未移行経路の回帰を含む。
- `make test-integration PYTEST_ARGS='-x -q -rs'`: **1338 passed、22 skipped**。追加した分析可能記事・補完待ち記事のrollbackと、元の例外・原因チェーンの監査テストも通過。一時Postgres・Redisは終了後に削除済み。
- 22件のskipはすべて既存の`tests/test_db_user_isolation.py`。`public.watchlist_entries`についてAlembic適用済みschemaを要求する条件による。
- 基準commit `e36168f83`の旧ソースと今回のソースを同じfixture・共通取得入口で実行し、7fixture・17記事すべての`FetchedArticle`が完全一致した。
- 静的型チェックは専用backend CLI・設定が未構成のため未実行。依存追加なし。

| fixture | 一致した記事数 |
| --- | ---: |
| `venturebeat_rss.xml` | 2 |
| `venturebeat_teaser_rss.xml` | 2 |
| `plos_one_atom.xml` | 3 |
| `microsoft_research_rss.xml` | 2 |
| `the_register_atom.xml` | 3 |
| `fierce_biotech_rss.xml` | 3 |
| `meta_ai_rss.xml` | 2 |

上記は固定fixtureによる比較であり、外部配信サイトへのlive取得は実行していない。
