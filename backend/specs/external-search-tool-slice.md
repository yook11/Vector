# External Search Tool slice 仕様

更新日: 2026-07-19

実装状況: Implemented（PR3）

## 位置付け

本sliceは、`agent-declaration-runner-orchestration-slice.md`のPR3を具体化する。

`TavilySearchProvider`を、完成済みqueryだけを実行するExternal Search Tool port / adapterへ移す。
PR2でDeepSeek function callingを「実行されないstructured-output transport」と定義したのと対で、
実際に外部I/Oを行う本物の実行能力へ正式なTool契約を与え、実行能力から`SearchProvider`語彙を外す。

`ExternalSearchResearchRunner`は一時的なworkflow ownerとしてToolを呼び続け、task / query policyは
本sliceで移さない。branch scopeのresource factoryはPR4で扱う。

前提: `external-query-selector-agent-runtime-slice.md`(PR2)の実装完了。

## Work Definition

### Problem

- 実行可能な検索能力が`SearchProvider`という旧語彙のままで、親仕様が定義するTool契約
  (stable name / typed input / typed output / invoke port / failure contract)が型に現れていない。
- PR2でtransport(DeepSeek function calling)とTool(実行能力)を区別したが、Tool側の正式契約が無いため、
  区別が語彙上で完成していない。
- Tool callの観測spanが無く、query単位の外部検索実行が回答trace上に現れない。

### Evidence

- `external_search/tavily.py`: `TavilySearchProvider.search(query, *, limit)`は完成済みqueryを受け、
  Tavily POST(topic=news / search_depth=basic / max_results=min(limit, 20) / include_answer=False /
  include_raw_content=False)を実行する。queryの生成・書き換え・言い換えはしない。
- transport timeoutは`TAVILY_REQUEST_TIMEOUT_SECONDS = 10`。`httpx.RequestError`は
  `tavily_search_http_error`、非2xxは`tavily_search_http_status_{code}`、JSON不正は
  `tavily_search_invalid_json`、results非listは`tavily_search_invalid_results`として
  `ExternalSearchProviderError`へ分類される。
- 正規化はadapter内で完結する: title必須(strip後空はdrop)、URLは`SafeUrl`検証(失敗candidateはdrop)、
  snippetはstrip + `CANDIDATE_SNIPPET_MAX_CHARS`(500) cap、published_atはISO / RFC2822 parse +
  naiveはUTC付与、source_nameはhostのwww.除去。返却は`candidates[:limit]`。
- `limit <= 0`はValueError、api_key空はconstructorでValueError。API keyはAuthorization headerだけに使い、
  log / 例外 / traceへ出さない。
- `external_search/runner.py`: workflow backstopは`PROVIDER_SEARCH_TIMEOUT_SECONDS = 15`の
  `asyncio.wait_for()`。`(ExternalSearchProviderError, TimeoutError)`をquery単位のprovider failureへ
  変換し、全query失敗時だけtaskを`provider_failed`とする。candidate poolの構築・URL dedupe・
  `EXTERNAL_SEARCH_CANDIDATES_PER_QUERY`(10)のcapはRunner側にある。
- `external_search/contract.py`: `SearchProvider` Protocolは`search(query, *, limit) ->
  list[ExternalSearchCandidate]`だけを持つ。`ExternalSearchCandidate`はfrozenで`url: SafeUrl`を持つ。
- Tavily HTTP clientは`make_safe_async_client()`で生成され、compositionの回答graph構築側が所有する。
  PR3ではこの位置を変えない。
- LogfireのHTTPX instrumentationは`capture_all=False`とrequest / response body・header個別設定の
  両方を明示し、環境変数でcaptureが再有効化されないようにする。

### Invariants

#### Tool契約

- `ExternalSearchTool`は完成済みqueryを入力に受け、queryを生成・拡張・言い換えしない。
- Toolは検索結果の回答適合性を判断せず、正規化済みcandidateを返すだけとする。evidence選別は
  Selector Agent、候補pool構築はworkflow ownerの責任のまま。
- Tool契約はstable name、typed input、typed output、invoke port、failure contractの5点で構成する。

```python
ExternalSearchToolName = Literal["external_search"]


@dataclass(frozen=True, slots=True)
class ExternalSearchToolInput:
    query: str
    limit: int


class ExternalSearchTool(Protocol):
    @property
    def name(self) -> ExternalSearchToolName: ...

    async def invoke(
        self,
        input: ExternalSearchToolInput,
    ) -> list[ExternalSearchCandidate]: ...
```

- outputは既存`list[ExternalSearchCandidate]`、failure classは既存
  `ExternalSearchProviderError`を維持する。`provider_failed` / `provider_failed_query_count`も
  既存workflow vocabularyとして本sliceでは残し、実行能力のport名としてだけ`SearchProvider`を削除する。
- `ExternalSearchProviderError.reason`はadapterが所有する閉じた安全なcodeとし、
  `tavily_search_http_error`、`tavily_search_http_status_{status}`、`tavily_search_invalid_json`、
  `tavily_search_invalid_results`だけを許す。任意の例外文字列やcauseをreasonへ採用しない。
- 分類済みerrorはtransport requestやprovider responseを持つ元例外を`__cause__` / `__context__`に
  保持せず、公開する例外objectからsecretや本文へ到達できないこと。
- Tool stable nameは`external_search`とする。provider名(tavily)はadapter実装の詳細であり、
  Tool名に埋め込まない。
- model向けdescription、JSON schema registry、Agent宣言への`tools` field追加を行わない。
  model-driven tool selectionを採用しない設計では、modelへToolを宣伝する仕組みは不要である。

#### 責任の分離(いずれも既存挙動の固定)

- adapter所有: HTTP transport、transport timeout(10秒)、HTTP error translation、response正規化、
  `SafeUrl`検証、API secretの非漏洩、Tool call span。
- workflow owner所有(不変): query単位のbackstop timeout(15秒 `wait_for`)、query単位typed failure変換、
  全query失敗時の`provider_failed`、candidate pool構築、query件数・candidate件数cap。
- transport timeoutとworkflow backstopは責任が異なるため、1つの定数へ統合しない。
- Tavily request body、URL、max_resultsの丸め、正規化規則、reason文字列を変更しない。
- productionのExternal Search Tool adapterは同じspan契約を実装する。fake Toolに観測責任は要求しない。

#### Tool call span

- adapterは、入力preconditionを満たして開始したTool call 1回につき`external_search_tool_call` spanを1本、
  SpanKind `CLIENT`で開く。`limit <= 0`はprogramming errorとしてspan開始前にrejectする。
- span名は固定し、識別は属性で行う。独自attribute allowlistは
  `tool_name`(=`external_search`)と成功時の`candidate_count`だけとする。
- 分類済み失敗では標準`error.type`へ`ExternalSearchProviderError.reason`を記録し、
  descriptionなしのERROR statusでspanを閉じてからraiseする。exception eventを作らない。
- Logfire / OpenTelemetryが自動付与する内部attributeと、HTTPX子spanの標準HTTP attributeは、
  Tool spanの独自attribute allowlist比較対象外とする。LLM callではないためTool spanに`gen_ai.*`を使わない。
- workflow backstopまたは上流cancelでは`CancelledError`を捕捉・変換せず、成功値、`candidate_count`、
  `error.type`を捏造しない。Logfire既定のexception event / statusは許容し、自由文はexport redactionに委ねる。
- span attribute、event、status descriptionへquery、candidate URL / title / source_name / published_at /
  snippet、provider response body、API keyを記録しない。HTTPX instrumentationでもbody / headerをcaptureしない。

### Non-goals

- task / query concurrency、candidate pool、partial failure policyを動かすこと(PR9)。
- Tavily clientの生成・共有・close ownerを変えること(PR4)。
- `ExternalSearchResearchRunner`の削除、workflow ownershipの移動(PR5 / PR9)。
- 検索providerの追加・変更、Tavily request内容・reason分類の変更。
- rate limitの新規適用。
- provider由来titleの新しい長さ上限や既存prompt semanticsを本sliceで変更すること。

### Done

- 実行能力が`ExternalSearchTool` port + Tavily adapterとして5点契約で読め、実行能力に対する
  `SearchProvider`語彙が実行コードから消える。
- workflow ownerはfake Toolへ差し替えてtask policyをテストでき、Tavilyの具象型を知らない。
- query単位のTool callが`external_search_tool_call`として同じ回答trace配下に観測でき、
  検索本文・candidate metadata・provider response・secretがtrace / logへ漏れない。
- 既存Tavily regression(正規化・error分類・timeout・上限)が通る。

## 責任境界

| 責任 | ExternalSearchTool port | Tavily adapter | ExternalSearchResearchRunner |
|---|:---:|:---:|:---:|
| typed input / output / failure契約 | ○ | 実装 | - |
| HTTP transport / transport timeout | - | ○ | - |
| error translation / reason分類 | - | ○ | - |
| response正規化 / SafeUrl検証 | - | ○ | - |
| Tool call span | 契約 | ○ | - |
| backstop timeout / query単位failure | - | - | ○ |
| candidate pool / cap / URL dedupe | - | - | ○ |

配置: portと`ExternalSearchToolInput`は`external_search/contract.py`、adapterは
`external_search/tavily.py`の`TavilyExternalSearchTool`へ改名して置く。`agent/tools/`等の
新packageは、複数consumerが現れるまで作らない。

## Test contract

- Tool portのstable name、typed input / output、`invoke(input)` signatureをcontract testで固定する。
- Tool 1 invocationにつきTavily POSTが1回で、queryを書き換えずに送信する。
- `limit`の丸め(min(limit, 20))、`limit <= 0`のValueError、candidate `[:limit]` capを維持する。
- title欠落・SafeUrl検証失敗のcandidateがdropされ、snippet cap・published_at parse・source_name
  正規化が既存と同値である。
- `httpx.RequestError` / 非2xx / JSON不正 / results非listが既存reasonの
  `ExternalSearchProviderError`になる。任意reasonはrejectし、traceの`error.type`へ流れない。
- workflow ownerがfake Toolで差し替え可能で、clean済みqueryとlimitを`ExternalSearchToolInput`で渡し、
  15秒backstop・query単位failure・全query失敗時の`provider_failed`が既存どおり発火する。
- production Tool call 1回につき`external_search_tool_call` spanが1本、許可済みattributeだけを持つ。
- 分類済み失敗でspanに安全な`error.type`とdescriptionなしerror statusがあり、exception eventが無い。
- backstop timeout / cancellationで成功属性を捏造せず、cancellationを変換しない。
- Tool spanとHTTPX子spanを含むexport対象trace全面(attribute / event / status description)および
  adapter logにquery・candidate metadata・provider response・API key sentinelが存在しない。
- `LOGFIRE_HTTPX_CAPTURE_ALL=true`の環境でもHTTPX子spanの存在とTool spanへの親子接続を確認し、
  request / response body・headerのsentinelがexportされない。
- transport / JSON decodeの分類済みerrorは`__cause__` / `__context__`に元例外を保持しない。
- ambientな`agent_answering_run` span下でTool spanが同一traceへ接続される。
- 既存external search regression(query生成〜task report)が通る。

Exit gate: 親仕様`AT-01`〜`AT-04`、`ERR-02`〜`ERR-04`、`REG-05`、`REG-09`。

残すseam: `ExternalSearchResearchRunner`、Tavily clientの現行生成位置、provider failure vocabulary。
削除するseam: 実行能力に対する`SearchProvider`語彙。
