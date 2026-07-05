# External search Tavily provider slice 仕様

## 位置付け

`SearchProvider` port の実 provider 実装として Tavily Search API adapter を追加する
slice。前提: external-search-research-runner-slice(runner 骨格 + fake port)が
実装済みであること。

provider は 2026-07-04 の比較リサーチで Tavily に決定済み。決定理由:
ToS が evidence の保存・LLM 入力に唯一クリーン / `topic=news` で published_date
が返る / 日付範囲フィルタあり / 本用途の呼び出し量(最大 9 検索/質問)なら
無料枠 1,000 回/月で足りる / REST 1 POST で SDK 依存が不要。
乗り換え先 1 号は SerpAPI(published_date 欠損が多発した場合)。

QueryGenerator / EvidenceSelector(DeepSeek adapter)と composition root への
配線は後続 slice の責務とする。この slice が終わっても runner の実行系は
fake selector のままで、「Tavily に差し替え可能な部品が 1 つ完成する」が Done。

## Problem

runner の `SearchProvider` port には fake しか存在しない。実 provider を
与えるにあたり、次を同時に満たす必要がある。

- httpx / JSON の例外を port 境界から漏らさない(runner は
  `ExternalSearchProviderError` と TimeoutError だけを分類済み失敗として扱う)。
- 検索結果は untrusted input であり、壊れた field(不正 URL・過長本文・
  未知形式の日付)で処理を止めず、その 1 件だけを丸めて継続する。
- api key を log / 例外 message に露出しない。
- dev の docker backend は外向き egress が無いため、実レスポンスの検証は
  host 側の手動実行で行う。

また Tavily の `content` は長文が返り得るが、`ExternalSearchCandidate.snippet`
には長さ cap が無い。pool 20 件 × 長文で selector 入力長の構造 cap が実質
破られるため、candidate 側にも snippet cap が必要になる。

## Evidence

- `backend/app/agent/external_search/contract.py`
  - `SearchProvider` port は `search(query, *, limit) -> list[ExternalSearchCandidate]`。
  - `ExternalSearchCandidate` は url(SafeUrl) / title / snippet / published_at /
    source_name。snippet に max_length が無い(本 slice で追加)。
  - `ExternalSearchProviderError` が分類済み境界 error として定義済み。
- `backend/app/agent/external_search/runner.py`
  - runner は `limit=EXTERNAL_SEARCH_CANDIDATES_PER_QUERY(10)` で呼び、
    15 秒の backstop timeout と応答の防御的 truncate を既に持つ。
- `backend/app/config.py`
  - AI provider の api key は `SecretStr = SecretStr("")` パターン
    (gemini / openai / deepseek)。tavily_api_key も同型で追加する。
- 既存 AI adapter(`planning/ai/gemini.py` / `internal_retrieval/ai/gemini.py` /
  `analysis/assessment/ai/deepseek.py` ほか)
  - いずれも `__init__` で空 api key を検出して fail-fast する。
    本 adapter も同じパターンに従う。
- `backend/app/shared/security/safe_http.py`
  - `make_safe_async_client(**kwargs)`: SSRF 検証 + DNS pin 付き client factory。
    `follow_redirects=False` が既定。
  - `pyproject.toml` の ruff banned-api により `httpx.AsyncClient` の直接生成は
    禁止(lint で構造的に強制)。固定ホストでも safe client 経由が前提。
- `backend/app/shared/security/safe_url.py`
  - `SafeUrl` は http/https + 最大 2048 字 + IP literal の public 検証。
    DNS 解決は行わない(結果 URL を fetch しない本用途はこれで足りる)。
- `backend/pyproject.toml`
  - httpx は既存依存。新規 dependency は不要。
- Tavily API(リサーチ 2026-07-04、docs.tavily.com):
  - `POST https://api.tavily.com/search`、Bearer 認証、
    `max_results` は最大 20、basic search は 1 credit。
  - response `results[]` は `title / url / content / score` + `topic=news` 時
    `published_date`。**`published_date` の文字列形式は公式 docs 未記載**。
  - 媒体名 field は無い(URL hostname から導出する)。
  - dev tier rate limit 100 RPM(本用途の最大 9 検索/質問に対して十分)。

## Decision

`app/agent/external_search/tavily.py` に `TavilySearchProvider` を実装する。

```python
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_REQUEST_TIMEOUT_SECONDS = 10
TAVILY_MAX_RESULTS_LIMIT = 20


class TavilySearchProvider:
    """SearchProvider port の Tavily 実装。整形のみ行い、選別はしない。"""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        client: httpx.AsyncClient,
    ) -> None: ...

    async def search(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[ExternalSearchCandidate]: ...
```

- client は `make_safe_async_client()` で生成したものを注入する(adapter 内で
  生成しない、global singleton にしない)。lifecycle は呼び出し側が持つ。
  adapter の受け口は `post` のみの狭い Protocol(`TavilyHttpClient`)とし、
  `httpx.AsyncClient` がそのまま満たす(テストは MockTransport で結線)。
- timeout は request 単位で `TAVILY_REQUEST_TIMEOUT_SECONDS` を明示する
  (client 生成側の設定に依存しない。runner backstop 15s より内側)。
- api key は `SecretStr` で受け取り、header 構築時のみ
  `get_secret_value()` する。
- 空 key は `__init__` で検出して fail-fast する(既存 AI adapter と同型)。
  401 を Tavily に投げに行く経路を作らない。
- `limit <= 0` は ValueError を raise する(HTTP は呼ばない)。呼び出し元は
  module 定数(10)を渡す設計であり、非正の limit は設定ミスではなく
  プログラミングバグなので、`[]` や clamp で沈黙させず未分類例外として
  伝播させる(runner の「未分類の例外は握らず伝播」と同じ規律)。

request body は固定ポリシー:

```python
{
    "query": query,
    "topic": "news",
    "search_depth": "basic",       # advanced は 2 credit、v1 は不要
    "max_results": min(limit, TAVILY_MAX_RESULTS_LIMIT),
    "include_answer": False,
    "include_raw_content": False,
}
```

response 整形規則(list 順 = provider rank を維持し、`limit` 件に truncate):

| Tavily field | 変換 | 異常時 |
| --- | --- | --- |
| `url` | `SafeUrl` 検証 | 検証失敗はその result だけ drop |
| `title` | strip | 空は drop(title は evidence 必須 field) |
| `content` | strip + `CANDIDATE_SNIPPET_MAX_CHARS` truncate → snippet | 空は `snippet=None` |
| `published_date` | 寛容 parse(ISO 8601 優先) | parse 失敗・欠損は `published_at=None` で候補を残す |
| (無し) | `source_name` = url hostname(先頭 `www.` 除去) | — |

`contract.py` に `CANDIDATE_SNIPPET_MAX_CHARS = 500` を追加し、
`ExternalSearchCandidate.snippet` に `max_length` を焼く(adapter は truncate
してから構築、直接構築の超過は ValidationError で大きく見える)。

失敗分類: 非 2xx / httpx transport error / request timeout / JSON decode 不能 /
`results` が list でない response は、すべて `ExternalSearchProviderError` に
変換して raise する。例外 message には分類(status code 等)のみ載せ、
api key・request body・response body を含めない。

`results` が空 list の正常応答は空 list を返す(エラーにしない。
「検索したが候補なし」は runner 側で succeeded / candidate 0 として扱われる)。

retry / rate limiter は持たない。失敗は runner の query 単位の部分失敗機構に
分類済み error として渡すだけにする。

settings に `tavily_api_key: SecretStr = SecretStr("")` を追加する。
値の設定(local / fly secrets)はユーザー側で行う。

### 実レスポンス検証(手動)

`backend/scripts/probe_tavily_search.py` を追加する。host 側で実行し、
`app.config` の settings 経由で key を読み、`topic=news` の実レスポンスから
`published_date` の実形式・欠損率を確認する。確認した実形式は parse テストの
fixture に反映する。raw response をファイル保存・コミットしない。

実装順: adapter + unit tests(MockTransport)を先行し、probe は key 設定後の
最終 gate として実行する。probe と fixture 反映が済むまで slice を完了扱いに
しない(unit tests が green でも Done ではない)。

probe 実測結果(2026-07-05): `published_date` は RFC 1123 形式
(`Fri, 03 Jul 2026 16:10:52 GMT`)。`parsedate_to_datetime` fallback で
parse 可能なことを確認し、実測 sample を parse テスト fixture に反映済み。
ISO 系 fixture は将来の形式変更への耐性として残す。

## Invariants

- `SearchProvider` 境界から httpx / JSON の例外型を漏らさない。失敗は
  `ExternalSearchProviderError` のみ。
- api key を log・例外 message・repr に露出しない(`SecretStr` を維持し、
  取り出しは header 構築の 1 箇所だけ)。
- raw response body を log / audit に載せない。
- 候補の url / title / snippet / published_at / source_name は Tavily response
  由来のみ。adapter は整形だけを行い、選別・並べ替え・補完をしない。
- 壊れた result は その 1 件だけ drop(不正 URL・空 title)または field を
  None に丸める(日付 parse 失敗)。応答全体を失敗にしない。
- `published_at` が取れないことは候補を落とす理由にしない。
- `httpx.AsyncClient` を直接生成しない(`make_safe_async_client` 経由のみ)。
- `max_results` は 20 を超えて要求しない。
- 応答件数が `limit` を超えても `limit` 件に truncate する。

## Non-goals

- `target_time_window` → Tavily 日付フィルタ(`time_range` / `start_date`)の
  写像。自由な日本語の時間表現の正規化が必要になるため、slice 4 の
  QueryGenerator に正規化済み time_range enum を出力させて port を拡張する
  将来案として記録する。v1 は `topic=news` の鮮度バイアス + selector の
  as_of 判断に任せる。
- retry / rate limiter / circuit breaker。
- composition root への配線(QueryGenerator / EvidenceSelector が揃う統合 slice)。
- SerpAPI fallback adapter の実装(乗り換え条件の記録のみ)。
- URL 正規化(tracking param 除去)。cross-task dedupe の精度向上が必要に
  なった時点で検討する。
- 新規 dependency の追加。

## Behavior

```text
TavilySearchProvider.search(query, limit=10)
  limit <= 0: ValueError(HTTP を呼ばない)
  body = 固定ポリシー + query + max_results=min(limit, 20)
  response = client.post(TAVILY_SEARCH_URL, headers=Bearer, json=body,
                         timeout=TAVILY_REQUEST_TIMEOUT_SECONDS)
    - httpx error / timeout / 非 2xx / JSON 不能 / results 非 list:
        ExternalSearchProviderError(分類のみ、本文なし)
  candidates = []
  for result in results:            # 返却順 = provider rank
    url = SafeUrl 検証(失敗: この result を skip)
    title = strip(空: skip)
    snippet = content strip + 500 字 truncate(空: None)
    published_at = 寛容 parse(失敗・欠損: None)
    source_name = hostname(www. 除去)
    candidates.append(ExternalSearchCandidate(...))
  return candidates[:limit]
```

## Tests

Unit tests only。`httpx.MockTransport` で完結し、実 network は呼ばない。

1. 構築と入力 guard
   - 空 api key での構築は fail-fast(ValueError)。
   - `limit=0` / `limit=-1` は ValueError になり、HTTP が呼ばれない。
2. request 構築
   - URL / `Authorization: Bearer` header / `topic=news` /
     `search_depth=basic` / `include_answer=False` が送られる。
   - `max_results == limit`、`limit=30` のとき 20 に clamp される。
3. response 整形
   - happy path: url / title / snippet / published_at / source_name が
     candidate に写り、返却順が維持される。
   - `source_name` の `www.` 除去。
   - `content` が 500 字に truncate される(contract の max_length と一致)。
   - `content` 空・欠損は `snippet=None`。
4. published_date の寛容 parse
   - ISO 8601(tz あり / なし)を parse できる。
   - 未知形式・欠損は `published_at=None` で候補が残る。
   - 実形式 fixture(probe script で確認後に追加)を parse できる。
5. 部分 drop
   - SafeUrl 検証に落ちる url(非 http scheme / private IP literal)の
     result だけ drop され、他は残る。
   - 空 title の result だけ drop される。
6. 失敗分類
   - 非 2xx(401 / 429 / 500)、transport error、timeout、JSON decode 不能、
     `results` 欠落は `ExternalSearchProviderError` になる。
   - 例外 message に api key と response body が含まれない。
7. 正常な空応答
   - `results: []` は空 list を返し、エラーにならない。
8. contract 変更分
   - `ExternalSearchCandidate` の snippet が `CANDIDATE_SNIPPET_MAX_CHARS`
     超で直接構築されると ValidationError。

## Done

- `TavilySearchProvider` + contract の snippet cap + settings の
  `tavily_api_key` が実装され、上記テストが green(`/check` で検証)。
- probe script の host 実行で `published_date` の実形式を確認し、
  parse テストの fixture に反映済み(実形式未確認のまま完了にしない)。
- runner が fake provider を `TavilySearchProvider` に差し替えるだけで
  動く状態(配線自体は後続 slice)。
- 新規 dependency の追加なし。retry / rate limiter / 配線は未実装のまま。
