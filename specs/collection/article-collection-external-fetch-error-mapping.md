# HTTP status → ExternalFetchError 変換を collection 直下へ集約し fallback 分類を整理する

Status: Implemented

## 目的

`ExternalFetchError` を生成する変換ロジック (HTTP status / httpx 例外 → origin error)
を、collection 共通概念として読める位置・名前に直し、あわせて表外 status の fallback
分類を status-class で割って `retryable`(失敗性質の SSoT) を正す。

現状の問題は 2 つ:

1. **位置・命名**: 変換 module が `article_acquisition/tools/http_error_translation.py`
   に置かれ、completion (`article_completion/scraper.py`) が acquisition の内部 `tools/`
   を import する cross-stage 依存になっている。エラー定義 `external_fetch_errors.py` は
   既に `collection/` 直下 (共通) なのに、それを生成する factory だけ sub-BC に埋まる捻れ。
   関数名 `classify_fetch_status` / `translate_fetch_exception` も非対称で、どちらも
   `ExternalFetchError` を返すのに戻り値を名乗らない。

2. **fallback 分類が粗い**: 表外 status が一律 `FetchUnexpectedStatusError`
   (`retryable=True` 固定) に倒れる。`retryable` が `ClassVar` のため、同一 class 内で
   400 (terminal) と 507 (retryable) を区別できない。結果として恒久的な 4xx client error
   が「再実行で変わりうる」扱いになり、無駄な retry と失敗の見えにくさを生む。

## Problem / Invariants / Non-goals / Done

### Problem

HTTP status / httpx 例外から `ExternalFetchError` への変換が acquisition 配下にあり、
collection 共通概念として読みにくい。さらに fallback status の分類も粗い。

### Invariants

- `ExternalFetchError.retryable` は失敗性質 (再実行で結果が変わりうるか) の SSoT。
- completion / acquisition は httpx 例外を直接判断しない (変換に委ねる)。
- HTTP status → `ExternalFetchError` の写像は collection 共通の一箇所に集約する。
- 外部取得は `follow_redirects=False` が共通 policy。3xx は全経路で terminal に倒す。
- Location header (token を含みうる) を error message / metric attribute に載せない。

### Non-goals

- completion / collection の processing_outcome metric 実装はしない (別タスク)。
- retry policy を変えない。`pipeline_events` の schema / 列 / payload 構造 / `outcome_code`
  の DB CHECK を変えない。ただし **outcome_code の語彙 (CODE の値集合) は変わる** —
  詳細は [outcome_code 語彙の変更](#outcome_code-語彙の変更-非破壊) 参照。
- DB / migration を触らない。
- `Stage` enum・API response shape を変えない。
- 個別 status の専用 class 追加 (413 等) はしない。区別を要する consumer が出るまで保留。

### Done

- 変換 module を `app/collection/external_fetch_error_mapping.py` へ移動。
- 関数名を `external_fetch_error_from_http_status` / `external_fetch_error_from_exception`
  に変更。例外側の引数 `source_name` は実態と乖離 (completion は URL を渡す) のため
  `target_label` に改名。
- 全 import を新しい場所・名前へ更新。
- status fallback を client / server / redirect に整理し、`retryable` を status-class で正す。
- completion scraper の手動 3xx 分岐を削除し、`raise_for_status()` → 変換に一本化。
- `FetchRedirectBlockedError` に optional `status_code` を持たせる (Location は持たせない)。
- 既存テストを新しい契約 (位置・名前・分類) で更新。test ファイル名も追従。

## Evidence (現状)

| 項目 | 実装 | 参照 |
|---|---|---|
| 変換 SSoT | `classify_fetch_status` / `translate_fetch_exception` | `app/collection/article_acquisition/tools/http_error_translation.py` |
| エラー定義 (既に共通位置) | `ExternalFetchError` family | `app/collection/external_fetch_errors.py` |
| 表外 fallback | `FetchUnexpectedStatusError` (`retryable=True` 固定) | `external_fetch_errors.py:250` / `http_error_translation.py:81` |
| 変換の利用 (acquisition) | rss / algolia_hn / crossref reader, `raw_http_client.py` | 各 `reader/*.py`, `tools/raw_http_client.py` |
| 変換の利用 (completion) | `scraper.py` が `raise_for_status()` → 変換、ただし 3xx は手動分岐 | `article_completion/scraper.py:292-310` |
| docstring 参照 | path を本文で言及 | `app/shared/security/ssrf_guard.py` |
| redirect policy | `follow_redirects=False` 既定 | `app/shared/security/safe_http.py:108` |

### scraper の 3xx 手動分岐は誤った前提に基づく

`scraper.py:293` のコメント「3xx は `raise_for_status` で拾われないため明示的に弾く」は
誤り。httpx の `raise_for_status()` は `is_success` (2xx) 以外を全て `HTTPStatusError`
として raise する (3xx に "Redirect response" 分岐を持つ)。よって手動分岐 (295-305) は
冗長で、削除しても `raise_for_status()` が 3xx を raise し、`except` → 変換が拾う。

## 設計

### A. 変換 module の移動・改名

| 項目 | 現状 | 変更後 |
|---|---|---|
| module | `app/collection/article_acquisition/tools/http_error_translation.py` | `app/collection/external_fetch_error_mapping.py` |
| 関数 (status) | `classify_fetch_status(status_code, headers)` | `external_fetch_error_from_http_status(status_code, headers)` |
| 関数 (例外) | `translate_fetch_exception(exc, *, source_name)` | `external_fetch_error_from_exception(exc, *, target_label)` |
| test | `tests/collection/test_http_error_translation.py` | `tests/collection/test_external_fetch_error_mapping.py` |

- httpx 依存の変換は、httpx 非依存の純粋なエラー定義 `external_fetch_errors.py` と
  **同居させず別ファイル**に分ける (定義モジュールに transport 依存を持ち込まない)。
- import 更新 (prod 5): `article_completion/scraper.py`, `reader/rss_reader.py`,
  `reader/algolia_hn_reader.py`, `reader/crossref_reader.py`, `tools/raw_http_client.py`。
- import 更新 (test 1): 改名後の `test_external_fetch_error_mapping.py`。
- 引数改名の呼び出し側更新: `external_fetch_error_from_exception` の `source_name=` →
  `target_label=` を 5 prod 呼び出し全てで更新 (scraper は `url_str`、各 reader /
  raw_http_client は source 名を渡す。共通層なので中立名にする)。
- docstring 参照更新 (1): `app/shared/security/ssrf_guard.py` 本文の path。
- audit (`audit/stages/{acquisition,completion}.py`) は `ExternalFetchError` 型のみ
  import するため無影響。

### B. status fallback の分類整理

#### 明示マッピング (据え置き・変更なし)

| status | class | retryable | 備考 |
|---|---|---|---|
| 401 / 403 | `FetchAccessDeniedError` | False | reason=unauthorized / forbidden |
| 404 / 410 | `FetchResourceNotFoundError` | False | reason=not_found / gone |
| 451 | `FetchLegalBlockError` | False | |
| 408 | `FetchRequestTimeoutError` | True | 4xx だが retry 可 (明示) |
| 425 | `FetchRetryableStatusError` | True | reason=too_early (明示) |
| 429 | `FetchRateLimitedError` | True | Retry-After 尊重 (明示) |
| 500 / 503 | `FetchOriginServerError` | True | reason=internal_error / service_unavailable |
| 502 / 504 | `FetchGatewayError` | True | |

明示表の役割 = 「status-class の既定から外れるもの (408/425/429 は 4xx だが retryable) 、
または distinct な CODE / metadata が要るもの」。

#### fallback (変更: status-class で割る)

`status_code // 100` で分岐:

| range | class | retryable |
|---|---|---|
| 3xx | `FetchRedirectBlockedError` (既存を再利用, status_code 付与) | False |
| 5xx | `FetchUnexpectedServerStatusError` (新規) | True |
| 4xx / 1xx / 範囲外 (それ以外) | `FetchUnexpectedClientStatusError` (新規) | False |

擬似コード (明示マッピングの後段):

```python
status_class = status_code // 100
if status_class == 3:
    return FetchRedirectBlockedError(status_code=status_code)  # Location は読まない
if status_class == 5:
    return FetchUnexpectedServerStatusError(status_code=status_code)
# 4xx + 1xx + 範囲外 (600/700 等)。2xx 成功でも 3xx/5xx でもない分類不能 status を terminal に倒す
return FetchUnexpectedClientStatusError(status_code=status_code)
```

#### 代表 status の落ち先 (= 振る舞いの変化)

| status | 現状 | 変更後 | retryable |
|---|---|---|---|
| 400 / 409 / 413 / 418 / 422 | `FetchUnexpectedStatusError` | `FetchUnexpectedClientStatusError` | True → **False** |
| 301 / 302 / 307 / 308 | `FetchUnexpectedStatusError` (raw 経路) / 手動 `FetchRedirectBlockedError` (scraper) | `FetchRedirectBlockedError` (status_code 付き) | True/False → **False** (全経路統一) |
| 501 / 505 / 507 / 508 / 520-524 | `FetchUnexpectedStatusError` | `FetchUnexpectedServerStatusError` | True → True (据置) |
| 100 / 101 / 199 / 600 / 700 | `FetchUnexpectedStatusError` | `FetchUnexpectedClientStatusError` | True → **False** |

核心の挙動変化: **未マップの 4xx・3xx・分類不能 status が retryable=True → False に倒れる。
5xx は不変**。恒久 client error を無駄に retry せず、失敗性質を正しく表す。

#### 3xx を変換に一本化する理由

「redirect は追わない」という判断を各 fetcher に散らさず、HTTP status → `ExternalFetchError`
の共通変換に集約する。`follow_redirects=False` が共通 policy なので、3xx は全経路で
`FetchRedirectBlockedError` (terminal) に倒れて一致する。scraper の手動分岐は削除し、
`raise_for_status()` → 変換に委ねる。`status_code` で 301/302/307/308 の観測値は残すが、
Location header は token を含みうるため error message / attribute に載せない。

### C. `external_fetch_errors.py` の変更

- 追加: `FetchUnexpectedClientStatusError` (CODE `fetch_unexpected_client_status`,
  `retryable=False`, `status_code` 必須)。明示分類しない 4xx + 分類不能 status の terminal
  escape hatch。
- 追加: `FetchUnexpectedServerStatusError` (CODE `fetch_unexpected_server_status`,
  `retryable=True`, `status_code` 必須)。明示分類しない 5xx。
- 削除: `FetchUnexpectedStatusError` (retryable=True 固定の旧 escape hatch)。
- 変更: `FetchRedirectBlockedError` に optional `status_code: int | None = None` を追加し、
  `_default_message` は status_code があれば `f"{CODE}: HTTP {status_code}"`、無ければ
  `CODE` を返す (既存の引数なし構築互換を維持)。

concrete subclass 数: **18 → 19** (削除 1 / 追加 2)。

### outcome_code 語彙の変更 (非破壊)

origin の `CODE` は監査の `outcome_code` に投影される (acquisition は origin VO の
`code` を verbatim で焼き、completion も `ScrapeFailure` の `reason_code` = `exc.CODE` を
経て `outcome_code` に焼く)。よって本変更は **`outcome_code` の語彙 (値集合) を変える**:

- 消える値: `fetch_unexpected_status`
- 増える値: `fetch_unexpected_client_status` / `fetch_unexpected_server_status`
- 据え置き: `fetch_redirect_blocked` (3xx が新たにここへ流入。値自体は不変)

非破壊である根拠: `outcome_code` は自由文字列列で、過去の `pipeline_events` 行は旧値の
まま残る。DB schema / CHECK / payload 構造は不変で、変わるのは **future event が書く
語彙のみ**。`outcome_code` を enum 化・集計している consumer は現状いない (集計 metric は
別タスク) ため、追従が必要な下流もない。

### D. 変換以外のコード変更

- `article_completion/scraper.py`: 手動 3xx 分岐 (295-305) と `redirect_not_followed`
  INFO ログを削除。`is_fetch_allowed` → `client.get` → `raise_for_status()` の流れに統一し、
  3xx は `except (httpx.HTTPError, ...)` → 変換が `FetchRedirectBlockedError` に写す。
- `article_completion/scrape_failure.py`: `classify_external_fetch_error` の match arm から
  `FetchUnexpectedStatusError` を外す。`FetchUnexpectedServerStatusError` を retryable arm
  (`UNKNOWN` schedule, 旧 unexpected と同じ扱い) に追加。client / redirect は
  `retryable=False` なので冒頭 `if not exc.retryable` の early return で `ScrapeTerminal`。

## テスト変更

- **`test_external_fetch_error_mapping.py`** (旧 `test_http_error_translation.py` を改名):
  - 表 row を更新。`(400/418/422, FetchUnexpectedStatusError)` を削除し、
    `(400/409/413/418/422 → FetchUnexpectedClientStatusError, retryable=False)` /
    `(301/302/307/308 → FetchRedirectBlockedError, status_code 検証)` /
    `(501/507/520 → FetchUnexpectedServerStatusError)` /
    `(100/600 → FetchUnexpectedClientStatusError)` を追加。
  - import を新 module・新関数名に更新。
- **`test_external_fetch_error_codes.py`**:
  - `_EXPECTED_CODE_COUNT` 18 → 19、docstring の「計 19 種」と整合 (現 docstring は stale)。
  - `_RETRYABLE_CODES`: `fetch_unexpected_status` を `fetch_unexpected_server_status` に
    差し替え (8 件のまま)。
  - `_TERMINAL_CODES`: `fetch_unexpected_client_status` を追加 (10 → 11 件)。
  - `_CONSTRUCTION`: `FetchUnexpectedStatusError` を削除、新 2 class を追加、
    `FetchRedirectBlockedError` は `{}` 構築のまま (status_code optional)。
- **`tests/collection/article_completion/test_article_completion_scrape_failure.py`**:
  `FetchUnexpectedStatusError → UNKNOWN` を `FetchUnexpectedServerStatusError → UNKNOWN`
  (retryable) に置換し、`FetchUnexpectedClientStatusError` / `FetchRedirectBlockedError` が
  `ScrapeTerminal` になることを追加。
- **`tests/collection/article_completion/test_scraper.py`**: `test_3xx_redirect_returns_
  fetch_failed` (現 `"redirect not followed" in str(result)` を期待) を新契約に更新。
  手動分岐削除後も 3xx が `FetchRedirectBlockedError` になること、`result.status_code == 302`
  であること、**Location (`169.254.169.254`) が `str(result)` に出ない**こと (token 非漏洩) を
  assert する。message 期待は origin 既定 message (`fetch_redirect_blocked: HTTP 302`) に直す。

## 検証

`/check` の backend 契約に従う (pyright は使わない)。

```bash
# unit (ruff lint + format check + unit pytest)
cd backend && uv run ruff check app/ && uv run ruff format --check app/ && uv run pytest tests/ -x -q
# DB integration (backend 変更で必須)
make test-integration
```

- 変更ファイルを明示する場合の対象テスト: `tests/collection/test_external_fetch_error_mapping.py`,
  `tests/collection/test_external_fetch_error_codes.py`,
  `tests/collection/article_completion/test_article_completion_scrape_failure.py`,
  `tests/collection/article_completion/test_scraper.py`, および reader 既存テスト (回帰確認)。
- ruff は変更ファイルを明示列挙して実行する (tree 全体への書込で過去 migration を汚染しない)。

## 留意 (今回やらない)

- 501 / 505 の terminal 化、413 の専用 class 化は consumer が要るまで保留 (Non-goals)。
- collection processing_outcome metric (`retryable` を infra / failed バケットに割る) は
  本 taxonomy 修正を前提に別 PR。`retryable` を正すことがその metric の分母の正しさになる。
