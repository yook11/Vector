# ExternalResearchRuntimeFactory slice 仕様

更新日: 2026-07-19

実装状況: Implemented（PR4）

## 位置付け

本sliceは、`agent-declaration-runner-orchestration-slice.md`のPR4を具体化する。

external検索専用の資源(DeepSeek client、Tavily client)を、external branchが実際に実行されるときだけ
生成し、branchの終了で確実に解放する`ExternalResearchRuntimeFactory`を導入する。PR1のplanner scope
factory(`activate_planner_runtime`)と同じ「compositionが所有するasync context-manager factory」の型を、
branch scopeへ拡大適用する。

本sliceは、PR2が明記して受け入れた2つのresource debt——DeepSeek clientのclose owner不在と、
direct / internal pathでのclient object生成——をここで返済する。

前提: PR2(`external-query-selector-agent-runtime-slice.md`)とPR3(`external-search-tool-slice.md`)の
実装完了。merge後にEvidenceを現物と照合してから実装に入る。

## Work Definition

### Problem

- DeepSeek client(Query用 / Selector用)はadapter構築時に生まれ、誰もcloseしない。
- Tavily clientは`_DeferredQuestionAnsweringAgent.answer()`の`async with`で開閉される一方、
  DeepSeek clientは無管理であり、external資源のlifecycleが2系統に割れている。
- 現行の回答graphはplanning結果の確定前にexternal componentを構築するため、direct / internal path
  でも使われないclient objectが生成される。
- external資源の境界がworkflow owner移動(PR5 / PR8)の前に固定されておらず、移動時に資源管理まで
  同時に動かすことになる。

### Evidence

- `composition.py::_DeferredQuestionAnsweringAgent.answer()`は`make_safe_async_client()`を
  `async with`で開き、Tavily clientをgraph構築へ渡す。回答Runごとに開閉され、全経路でcloseされる。
  この開閉はdirect / internal pathでも発生する。
- `composition.py::_build_external_search()`はDeepSeek adapter(PR2後はQuery用 / Selector用の
  client + Runtime)を回答graph構築時に生成する。close処理は存在しない。
- `composition.py::ensure_question_answering_agent_configured()`がDeepSeek / Tavilyのcredentialを
  検証し、欠けていればrun作成前に`AIProviderConfigurationError`(開始APIの503)となる。
- `composition.py::activate_planner_runtime()`が、composition所有のasync CM factoryでclientを生成し
  Runtimeとして貸し出す前例を確立している(PR1)。
- `external_search/service.py::ExternalSearchService.search()`はtask空(その結果effective agent count 0)の
  とき早期returnし、それ以外で`runner.search(request)`を呼ぶ。external実行の開始点はここにある。
- `external_search/runner.py`はtask間とquery間の並行に既定の`asyncio.gather()`を使う。既定のgatherは
  最初の未分類例外を即時伝播する一方、兄弟coroutineをcancelせず走らせ続ける。現行はclientが
  closeされないため孤児coroutineは無害に完走するが、scope closeを導入するとこの前提が崩れる。
- 現行`ExternalSearchService`は完成済みRunnerをconstructorで保持し、Runnerも
  query generator / search provider / selectorをconstructorで保持する。branch scopeで資源を生成する
  には、資源の受け渡しportを新たに確定する必要がある。
- `scripts/probe_question_answering.py`は貫通probe用にexternal資源を独自構築しており、
  factory導入のconsumer inventoryに含める必要がある。
- lock済み`openai 2.44.0`の`AsyncOpenAI`はasync context manager退出時に`close()`を呼ぶ。PR2実装は
  DeepSeek clientを`base_url=DEEPSEEK_BASE_URL`明示で構築している。
- 既存のAI SDK遅延import契約(`tests/test_lazy_ai_sdk_import.py`)が、composition系moduleのimportで
  provider SDKをloadしないことを固定している。
- PR2仕様は「close ownerを新設せず、resource debtを明記して受け入れ、PR4で解消する」と定めている。

### Invariants

#### Factory契約とbundle

- `ExternalResearchRuntimeFactory`はcompositionが実装するportとし、activateでexternal branch専用の
  資源束を貸し出す。

```python
class ExternalResearchRuntimeFactory(Protocol):
    def activate(self) -> AbstractAsyncContextManager[ExternalResearchRuntime]: ...


@dataclass(frozen=True, slots=True)
class ExternalResearchRuntime:
    query_runtime: AgentRuntime      # binding済みper-role instance
    selector_runtime: AgentRuntime   # binding済みper-role instance
    search_tool: ExternalSearchTool  # PR3のTool port
```

- DeepSeek clientは1つだけ生成し、Query / Selector両Runtimeで共有する(PR2の2 client体制の統合)。
  Runtime instanceはbinding 1:1のままrole別を維持する。
- 共有DeepSeek clientは既存の`DEEPSEEK_BASE_URL`、client timeout 20秒、SDK既定transport retryで
  構築する。base URL指定を落とすとDeepSeek keyが`AsyncOpenAI`の既定接続先(OpenAI)へ送信されるため、
  base URLの明示を必須の不変条件とする。
- Tavily clientはfactoryが`make_safe_async_client()`で生成し、Tool adapterへ渡す。
  `_DeferredQuestionAnsweringAgent`によるTavily client開閉seamは削除する。
- factoryは呼ばれるまでいかなるclientも生成しない。さらに、factory objectの構築と
  context manager objectの生成ではprovider SDKをimportせず、context entry時にだけloadする
  (既存のAI SDK遅延import契約に従う)。
- bundleのRunnerへの受け渡しはper-call注入とする。`ExternalSearchResearchRunner.search()`を
  `search(request, *, external: ExternalResearchRuntime)`へ変更し、Runner constructorは`events`
  だけを保持する。可変fieldの差し替えとscope内でのRunner再構築は行わない(前者はRun間混線、
  後者はServiceの具象依存を生むため)。

#### Scopeとactivation owner

- scopeはexternal branchの実行1回とする。activateの一時ownerは`ExternalSearchService`とし、
  早期return判定の後、`runner.search()`をscope内で実行する。ownerを`AnsweringRunner`へ移すのは
  PR8であり、本sliceでは移さない。
- direct / internal-only pathではfactoryをactivateせず、DeepSeek / Tavilyのclient objectを
  1つも生成しない。PR2が受け入れた「direct / internal pathでもclient objectが生成される」挙動は、
  本sliceで明示的に打ち消す。
- 同じ回答Runのexternal branch内では全task・全attemptが同じ資源束を共有し、別の回答Runとは
  client / scopeを再利用しない。
- credential fail-fastは変更しない。client生成は遅らせるが、credential検証は遅らせない。
  DeepSeek / Tavily keyが欠けていれば、従来どおりrun作成前に同じ503を返す。

#### Close契約(planner factoryの前例を再利用)

- client取得成功後、正常終了・分類済みfailure・想定外例外・task cancellation・
  client取得後のRuntime / Tool構築失敗の各経路で、async context managerの退出により
  各clientのcloseを1回だけ試行する。
- close自体の失敗は特別処理せず、close例外が本体例外を置き換え得るPython / SDK既定挙動を
  意識的に受け入れる(元の例外は`__context__`に残る)。合成・優先順位・retryは扱わない。
- `asyncio.shield()`による保護は行わない。二重cancellation・process強制終了・close失敗による
  未完了は保証外とし、OSの資源回収に委ねる。
- 複数clientのcloseは互いに独立に1回ずつ試行し、一方のclose失敗が他方のclose試行を妨げない。
- Runtime / Toolは借りたclientをcloseしない。close ownerはfactoryだけとする。

#### Scope終了とin-flight処理

- scope所有者は、scopeを退出する前に、scope内で起動した全coroutine(task間・query間のgather子)を
  完了またはcancelして合流させる。孤児coroutineが閉じたclientへ到達する経路を残さない。
- 未分類例外の発生時は、残る子をcancelして合流(cancel-and-await)した後に元の例外を再raiseする。
  兄弟のcancel結果・二次失敗で元の例外を置き換えない。
- これは未分類例外経路における意図的な挙動変更である(現行は兄弟coroutineが独立に完走する)。
  分類済みfailureはtask / query単位で従来どおり変換され、この契約の影響を受けない。
- factoryは自分のscope内で起動された子を知らないため、この合流はscope内のService / Runnerが
  所有する。

#### 変えないもの

- SDK既定のconnection pool設定を使い、接続数上限・keep-alive tuningを追加しない。
- Gemini(planner)のphase scopeとは統合しない。回答Run全体のGemini resource scopeは
  親仕様「PR7以降」の専用sliceで扱う。
- 検索結果、error shape、event順序、task report、API / DB / dependencyを変更しない。

### Non-goals

- workflow ownershipの移動(PR5)、factory activation ownerの`AnsweringRunner`移動(PR8)。
- external pipelineの展開と`ExternalSearchResearchRunner`の削除(PR9)。
- Gemini clientのrun-local scope統合(PR7以降の専用slice)。
- connection pool上限・keep-alive等の独自設定。
- close失敗時のretry・例外合成・優先順位制御(必要になった場合の専用slice)。
- credential検証時点・error shapeの変更。

### Done

- external / mixed pathのexternal branchでだけ3資源(共有DeepSeek client、Tavily client、Tool)が
  生まれ、branchの全終了経路で各clientのcloseが1回試行される。
- direct / internal-only pathでDeepSeek / Tavilyのclient objectが1つも生成されない。
- `_DeferredQuestionAnsweringAgent`由来のresource開閉seamが消え、probe scriptを含む全consumerで
  external資源のlifecycleがfactory 1系統に統一される。
- scope退出時にin-flightの子coroutineが残らず、close後のclient使用が構造的に起きない。
- 開始APIのcredential fail-fastと、既存external search regressionが維持される。

## 責任境界

| 責任 | composition(factory実装) | ExternalSearchService | Runtime / Tool |
|---|:---:|:---:|:---:|
| client生成・close | ○ | - | - |
| activate scopeの開始・終了 | - | ○(一時。PR8でRunnerへ) | - |
| 資源束の使用 | - | runnerへ引き渡し | ○(借用のみ) |
| credential fail-fast | ○(既存関数を維持) | - | - |

配置: factory portと`ExternalResearchRuntime`は`external_search/contract.py`、factory実装は
`composition.py`。親仕様のbundle表記(`agent_runtime`単数)は、PR2のper-role instance方式に合わせて
本sliceの複数形へ更新する。

consumer inventory: `ExternalSearchService`(production経路)に加え、
`scripts/probe_question_answering.py`(貫通probe)もfactory経由へ更新し、外部資源の独自構築を
残さない。

## Test contract

- direct / internal-only pathの回答Runで、DeepSeek / Tavilyのclient構築が0回である。
- context preparation / hook失敗で短絡した回答Runと、task空(その結果effective agent count 0)の
  早期returnでは、factoryのactivateが0回である。
- external branchで、factory activate前にclientが生成されず、activateは1 branchにつき1回である。
- branch内の全task、Selectorのattempt 1 / 2が同じclient identityを使い、Query Runtimeと
  Selector Runtimeへ渡るDeepSeek clientが同一identityである。
- 同じService / Runner / factory objectを2回の回答Runで使っても、各Runで新しいclientが生成され、
  Run間の再利用が無い。
- `search(request, *, external=...)`のper-call注入契約が固定され、Runner constructorが
  資源(client / Runtime / Tool)を保持しない。
- 共有DeepSeek clientがDeepSeek base URL・timeout 20秒で構築され、SDK既定transport retryの
  上書きが無い。
- composition系moduleのimportとfactory構築・context manager生成でprovider SDKがloadされない
  (既存import-footprint契約の拡張)。
- 正常終了・分類済みfailure・想定外例外・cancellationの各経路で、fake clientのcloseがちょうど
  1回呼ばれ、close成功時は元の終了経路を抑止しない。
- 部分構築失敗の各段(共有DeepSeek client取得後のQuery Runtime構築失敗 / Selector Runtime構築失敗 /
  Tavily client取得失敗 / Tool構築失敗)で、取得済みのclientだけがちょうど1回closeされる。
- 一方のclose失敗時にもう一方のcloseが1回試行されることと、close失敗が本体例外を置き換え得る
  Python既定の契約を、それぞれ独立のテストで固定する。
- 未分類例外の発生時、scope退出前に全子coroutineが完了またはcancel済みで、close後に共有clientを
  使う呼び出しが発生しない。再raiseされる例外が元の未分類例外である。
- Runtime / Toolの単体呼び出しがclientをcloseしない。
- DeepSeek / Tavily credential欠落時、開始APIが従来どおり503を返す(検証時点が遅延していない)。
- connection pool・keep-aliveの独自設定が存在しない。
- `scripts/probe_question_answering.py`が独自のexternal資源構築を残さず、同じfactory経由で動く。
- 既存external search regression(query〜task report〜evidence dedupe)が通る。

Exit gate: 親仕様`RES-01`〜`RES-06`、`ARCH-02` / `ARCH-03`。

残すseam: `ExternalSearchService`の一時的なactivation ownerと`ExternalSearchResearchRunner`。
削除するseam: `_DeferredQuestionAnsweringAgent`由来のresource開閉と、PR2が受け入れた
close owner不在・全pathでのclient生成。
