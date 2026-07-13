# Agent Research 継続実行・表示連続性 corrective slice 仕様

Status: Draft（2026-07-13）

## 位置付け

本仕様は、同じ Research thread へ質問を追加したときだけ回答生成に失敗する事象と、質問送信・
失敗確定・ソース確定の前後で画面が一瞬消えたように見える事象を、1つの run continuity 問題として
修正する corrective slice である。

参照する既存仕様:

- `backend/specs/agent-history-run-execution-slice.md`
- `backend/specs/agent-history-thread-ui-slice.md`
- `backend/specs/agent-question-context-preparation-slice.md`
- `frontend/specs/agent-research-live-ui-slice.md`
- `frontend/specs/research-thread-navigation-pending.md`
- `frontend/specs/research-workspace-layout-redesign.md`

本仕様は次の範囲だけを後発仕様として上書きする。

1. `research-workspace-layout-redesign.md` の wide sources 初期 open、desktop 復帰時の自動 open、
   source 0件時の empty aside 表示を廃止し、すべての source surface をユーザー操作開始にする。
2. completed を中心に定義されている stable presentation 契約を、failed
   （`errorCode=cancelled` を含む）と、その後の Server Component 収束までへ拡張する。
3. existing-thread submitのServer Actionとclient componentに重複しているrefreshを解消し、live terminalの
   client convergence ownerをrunごとに1つへ限定する。
4. local development の bind mount をコード反映保証とみなさず、標準の worker 再生成対象へ
   `worker-agent` を含める。

DB、REST API、Pydantic schema、SSE event、認証・認可、run の終状態、question context のドメイン契約は
上書きしない。競合時は、local worker のコード反映、same-thread submit / terminal RSC の表示連続性、
sources disclosure の初期値と所有権についてだけ本仕様を正とする。

## Work Definition

### Problem

1. local development では `backend/app` を `worker-agent` へ bind mount しているが、常駐 Taskiq worker は
   Python module を再読込しない。起動時に import した旧 contract と実行時に lazy import した新実装が
   同一プロセス内で混在し、保存済み履歴を読む follow-up run だけが `internal_error` になる。
2. 標準の `pipeline-restart` が参照する `WORKERS` に `worker-agent` が含まれないため、通常の再起動を
   実施しても上記の世代ずれが残る。
3. live failed では回答 slot 内に failure を表示する一方、RSC 後の persisted failed では failure を
   質問直下の status rail へ移し、回答 slot を削除する。この二段階の移動と高さ収縮が、画面が消えて
   再表示されたような跳躍を生む。
4. wide sources は初期 open で、thread route mount や compact から wide への復帰時に幅320pxの aside と
   composer spacerを即座に追加する。質問送信や source 到着と disclosure state が独立していないため、
   ユーザーが開いていないのに回答領域と composer が急に狭くなる。
5. existing-thread submit は Server Action の `revalidatePath()` 後に composer からも
   `router.refresh()` を呼ぶため、同じ更新に複数の RSC refresh owner が存在する。
6. 現在の test は live failed と persisted failed を別々に検証し、その間の RSC commit、source closed state、
   visual anchor、focus、DOM identity の連続性を1本の遷移として検証していない。

### Evidence

2026-07-13 の再現時に、次の因果関係を確認した。

1. `worker-agent` は source 変更前から稼働を継続していた。
2. `ThreadMessageSnapshot.missing_aspects` を追加した source は bind mount 経由で container から見えていたが、
   起動済み Python process 内の `ThreadMessageSnapshot` は旧定義のままだった。
3. `backend/app/queue/tasks/agent_run.py` は worker 起動時に snapshot と service を importする一方、
   `backend/app/agent/composition.py` は Gemini context adapter を run 実行時に lazy importする。
4. 新しい `backend/app/agent/question_context/ai/gemini_prompt.py` が旧 snapshot の
   `missing_aspects` を読んだため、worker log に次の例外が記録された。

   ```text
   AttributeError: 'ThreadMessageSnapshot' object has no attribute 'missing_aspects'
   ```

5. 新規 thread は履歴が空で snapshot の field access が発生せず、同じ worker でも成功した。
6. 現在の fresh Python process では `ThreadMessageSnapshot` が `missing_aspects` を持ち、同じ prompt rendering は
   成功するため、保存データ破損や request shape ではなく process 内の source revision 混在と判断できる。

関連実装:

- `docker-compose.yml`: worker 共通設定で `./backend/app:/app/app` を bind mount。
- `backend/supervisord/agent.conf`: reload なしの常駐 Taskiq worker。
- `Makefile`: `WORKERS := worker-fetch worker-analysis worker-insights scheduler` で `worker-agent` が欠落。
- `backend/app/agent/threads/contracts.py`: 現行 `ThreadMessageSnapshot.missing_aspects` contract。
- `backend/app/agent/threads/repository.py`: 保存済み履歴の snapshot projection。
- `frontend/src/features/research/components/ResearchThreadView.tsx`: persisted failure rail と answer slot の条件分岐。
- `frontend/src/features/research/components/ResearchThreadLiveBoundary.tsx`: live snapshot と terminal refresh。
- `frontend/src/features/research/components/ResearchSourcesPanel.tsx`: `inlineOpen=true` と wide 復帰時の再 open。
- `frontend/src/features/research/components/ResearchComposer.tsx`: submit 後の `router.refresh()`。
- `frontend/src/features/research/api/submit-research-question.ts`: mutation 後の `revalidatePath()`。

外部仕様の確認:

- [Taskiq CLI](https://taskiq-python.github.io/guide/cli.html) は開発用 `--reload` を提供するが、reload 用 extra が
  必要である。新規 dependency と本番共用 process 設定の変更を避けるため、本 slice では採用しない。
- [Next.js `revalidatePath`](https://nextjs.org/docs/app/api-reference/functions/revalidatePath) は Server Function
  から呼ばれ、現在表示中の対象 path を更新する。submit 成功後に同じ目的の client
  `router.refresh()` を重ねない。

### Invariants

1. `ThreadMessageSnapshot` は `role`、`content`、`missing_aspects` の現行 contract を維持し、既存 thread の
   assistant history を失わない。
2. localでbackend sourceを変更した後のmanual / E2E検証は、標準の`make pipeline-restart`で
   `worker-agent`を再生成したprocessだけを有効とする。再生成前のrun結果を変更反映の検証証拠にしない。
3. run の accepted / queued / running / completed / failed と assistant message の正本は既存どおりDB、
   live draft / activity の正本は検証済み live state とする。
4. failed terminal時は、`errorCode=cancelled`を含め未確定draftを即時に非表示とし、確定回答、source、
   missing aspectsへ昇格しない。
5. terminal 受理から persisted RSC 描画まで、同じ run の turn anchor と安全な固定 terminal 表示を
   途切れさせず、failure が0件または重複する paint frameを作らない。
6. question bubble の descendant は質問本文だけとし、queued / running / failure は bubble 外の run status rail
   に置く。
7. sources の内容はDB確定済み assistant messageだけから導出する。openへの遷移はユーザー操作だけとし、
   source count、run status、RSC refreshからopenを推測しない。threadまたはviewport modeが変わった場合は、
   incompatibleなsurfaceをclosedへ収束させてよいが、自動で再openしない。
8. same-thread の submit、live update、terminal、RSC refresh は、answer scroller、turn anchor、composer input、
   live controller、announcer、sources disclosure owner を意図せず remountしない。
9. background update は focus を移動せず、answer / source の scrollTop と既存96px visual anchor契約を守る。
10. existing-thread submitのServer Action成功後に同目的のclient refreshを重ねない。terminal収束のclient
    ownerはrunごとに1つとし、既存backoffに基づく直列retryは許可するが、複数のin-flight refreshや
    retry timerを作らない。
11. 内部例外、provider payload、質問・履歴本文を API error や visible failure 文言へ公開しない。
12. REST API、Pydantic schema、generated TypeScript types、DB、認証・認可を変更しない。

### Non-goals

- `ThreadMessageSnapshot` や question context の business contract を変更すること。
- failed run を自動 retry、completed へ書換え、または既存 failed row を修復すること。
- Taskiq hot reload、watcher、新規 dependencyを導入すること。
- production deploy revision の handshake や全 worker process topologyを再設計すること。
- 既存`QUEUES`全体をbroker registryと再照合・改名すること。
- SSE、polling、attempt epoch、generation、finalization、error code vocabularyを変更すること。
- cancel Server Actionとlive terminal refreshのarbitrationを変更すること。
- 新しい検索結果、画像、関連リンク、source grouping / dedupeを追加すること。
- `missingAspects` を検索結果へ再定義すること。
- sources の開閉状態をURL、cookie、localStorage、global storeへ永続化すること。
- animation、人工的 delay、minimum heightだけで跳躍を隠すこと。
- Research 以外の shell、navigation pending、sidebar layoutを再設計すること。
- traceback local などの log redactionを本 slice で扱うこと。

### Done

本仕様は次をすべて満たしたとき実装 Done とする。

- 標準のlocal pipeline再生成対象に`worker-agent`が含まれ、runtime restart probeでcontainer開始時刻の更新を
  確認できる。fresh processのfake worker integrationでは既存threadのfollow-up runがcompletedになる。
- Compose の全 `worker-*` service と scheduler が Makefile の運用集合に含まれることをtestで固定する。
- live failed（`errorCode=cancelled`を含む）からpersisted failedまで、同じturnとfailure railが空frame、
  重複、再移動なく連続する。
- wide / compact とも sources は初期 closed で、ユーザー操作以外では開かない。
- same-thread submit と terminal RSC 後も、ユーザーが選んだ sources の open / closed stateを維持する。
- existing-thread submit で同目的の `revalidatePath()` と `router.refresh()` が重複しない。
- answer panel、composer、sources、focus、scrollの必須 component / browser regression testが通る。
- 実装後に `/check` を完走し、migration と generated API type に差分がない。

## Unified Run Continuity Contract

run continuity は、backend task の完了可否だけでなく、submit accepted からDB終状態の画面収束までを
1つの連続した user-visible operation として扱う。

| 段階 | 正本 | 回答領域 | failure rail | sources surface | refresh owner |
|---|---|---|---|---|---|
| submit pending | composer local state | 既存表示を維持 | 既存状態 | 現在の開閉を維持 | なし |
| accepted / queued | thread detail DB | 新しい turn anchorを追加 | queued | 自動 openしない | submit Server Action |
| running / draft | live snapshot | draftまたは既存placeholder | running / activity | 内容・開閉を変更しない | なし |
| live completed | terminal live snapshot | finalizing表示を維持 | completed収束表示 | 自動 openしない | live convergence coordinator |
| persisted completed | thread detail DB | 同じturnのfinal answer | 不要な完了表示を除去 | source内容だけ更新 | 追加refreshなし |
| live failed | terminal live snapshot | draftを同commitで除去 | errorCode別の固定failureを同commitで表示 | 自動 openしない | live convergence coordinator |
| persisted failed | thread detail DB | 未確定回答を表示しない | 同じowner・同じ位置の固定failure | 自動 openしない | 追加refreshなし |

次を禁止する。

- submit accepted 後にServer Actionの更新と同目的のclient `router.refresh()`を追加する。
- terminal convergenceで複数のclient ownerがoverlapするrefreshまたはretry timerを作る。
- live terminal後に failure を answer slot から status railへ移動する。
- persisted terminal commitでturn wrapperを削除して作り直す。
- source countの0→正数、thread mount、hydration、RSC refreshを理由にaside / sheetを開く。
- final / failed RSCの前後でprotected route全体のloading shellを挟む。

## Dev Worker Source Coherence

### Supported local activation model

bind mount は「container filesystemから最新sourceを読める」ことだけを保証し、起動済みPython processの
import cache更新は保証しない。backend application source、設定、ORM modelの変更後は、次のResearch runを
検証する前に標準の `make pipeline-restart` でbackendと全常駐workerを再生成する。

`Makefile` の運用集合を次に固定する。

```make
WORKERS := worker-fetch worker-analysis worker-insights worker-agent scheduler
PIPELINE := backend $(WORKERS)
```

`QUEUES`には既存entryを変更せず`agent`を追加し、user-facing queueを`pipeline-status`で観測可能にする。
既存の全queue entryをbroker registryと再照合・整理することは本sliceの範囲外とする。

この集合を参照する次のtargetは、すべて `worker-agent` を含まなければならない。

- `pipeline-restart`
- `pipeline-down`
- `pipeline-logs` の既定対象
- `migrate-safe` の停止・再生成対象

`pipeline-restart` の説明は「設定/ORM変更後」ではなく「backend app code・設定・ORM変更後の再生成」とし、
bind mountだけでは常駐workerへ反映されないことを `docs/development.md` に記載する。

### Restart safety and recovery

1. local restart は意図した agent run が active でないことを確認してから行う。実行中runを強制停止して
   見かけ上の修復を行わない。
2. 今回すでに `failed/internal_error` になったrunは終状態のまま保持し、DBを書換えない。
3. worker再生成後、ユーザーは必要な質問を同じthreadへ再送できる。
4. recovery確認では `worker-agent` の開始時刻がsource変更後であることと、logに同じ
   `missing_aspects` AttributeErrorがないことを確認する。

限定復旧コマンドはdevelopment documentに記載してよいが、標準経路は `make pipeline-restart` とする。

```bash
docker compose up -d --force-recreate worker-agent
docker compose ps worker-agent
docker compose logs --since 5m worker-agent
```

### Rejected fallback

`getattr(snapshot, "missing_aspects", ())` のような application-level compatibility fallbackは追加しない。
このfallbackは今回のfield accessだけを通し、同一process内に残る他の新旧 contract 混在を検出できなくする。

Taskiq hot reloadは将来の選択肢だが、新規dependencyの事前合意、development限定configuration、reload中の
task safetyを別途定義してから扱う。本仕様のDone条件には含めない。

## Refresh Ownership

### Submit

1. mutationの正本は既存どおりServer Actionとする。
2. 新規threadはServer Actionの`redirect(/research/{threadId})`だけでrouteを確定する。
3. existing threadはServer Actionの`revalidatePath(/research)`と
   `revalidatePath(/research/{threadId})`でqueued stateを反映する。
4. submit成功後のcomposerは入力をclearするが、同じ目的の`router.refresh()`を追加で呼ばない。
5. submit失敗時は現在のthread、input、sources stateを維持し、既存のsafe toastだけを表示する。

### Terminal convergence

1. runごとに1つのlive convergence coordinatorだけがterminal RSC convergenceを所有する。
2. coordinatorは同時に1件を超えるrefresh requestまたはretry timerを持たない。SSEとpollingが同じterminalを
   観測しても重複したin-flight refreshを作らない。
3. DB反映が遅れてrefresh後も同じrunがactiveな場合は、既存の2 / 4 / 8 / 10秒上限の契約に従い、
   coordinatorが直列にretryしてよい。「ownerが1つ」は「attemptが1回だけ」を意味しない。
4. terminal refreshはthread detailをDB正本へ収束させるsignalであり、live payloadからfinal answer、source、
   missing aspectsを合成しない。
5. RSC commit後はactive run controllerを停止してよいが、turn wrapper、answer scroller、composer、
   announcer、sources disclosure ownerを同じthreadで作り直さない。
6. terminal refreshが失敗・遅延した場合も、live terminal presentationを維持し、空のanswer panelへ戻さない。

cancel mutationのrefresh arbitrationは本sliceで変更しない。`errorCode=cancelled`の表示連続性は本仕様で
検証するが、cancel action pending中のServer Action RSCとlive terminal refreshの調停は別sliceで扱う。
cancel 204から表示文言を決めず、既存どおりDB `status=failed, errorCode=cancelled`を正本とする。

## Research Presentation Boundaries

| Owner | 責務 | 所有しないもの |
|---|---|---|
| thread Server Component | persisted messages、run終状態、final answer、sources、missingAspects | draft、source開閉state |
| live controller | active runのstage、activity、draft、terminal signal | final answer、persisted source、layout preference |
| stable turn presentation | submitからterminal RSCまでのquestion、status rail、answer anchor | transport接続、DB write |
| sources disclosure owner | user-selected open / closed とresponsive surfaceの排他表示 | source生成、run status |
| sources list | assistant message単位のDB確定source | live draft、missingAspects |
| missing aspects block | 対応するfinal answerで確認できなかった観点 | 検索結果、source一覧 |
| answer scroll owner | 96px follow、visual anchor、`最新の回答へ` | source scroll、document scroll |

同じReact componentに複数の表示片が存在してもよいが、stateの導出元とlifecycle ownerを上表どおり分離する。
特にsources disclosure stateを`messages`、`activeRunId`、`sourceCount`、viewportから再初期化しない。

## Failed Presentation Contract

1. `status=failed` terminalを受理したcommitで、未確定draftをDOMから除去し、同じrunのstatus railへ
   safeなerrorCode別固定文言を表示する。cancelは独立statusではなく`errorCode=cancelled`として扱う。
2. live terminalとpersisted failedは、同じrun IDをanchorとする同一status rail componentで描画する。
3. terminal受理からDB反映までの全renderで、draftまたは固定failureのどちらか一方を表示する。
   terminal受理後にdraftを残さず、failureの空 frameや重複も作らない。
4. persisted RSC commitはfailureの文言、位置、owner、turn wrapperを変更しない。
5. answer slotをfailed時に閉じる場合は、failure railを表示する同じcommitで行う。後続RSC commitで
   追加の高さ収縮を起こさない。
6. failed contractionをanswer content revisionとしてscroll ownerへ通知し、既存96px契約を適用する。
   - 最下部から97px以上離れている場合の目標値は
     `min(previousScrollTop, newScrollHeight - clientHeight)`とし、browserが許すclamped scrollTopを維持する。
   - 96px以内の場合はfailed turn anchorのviewport位置を維持する。scroll range不足で不可能な場合は最も近い
     clamped位置を採用し、後続RSC commitでさらに移動させない。
   - browser roundingの許容差は、上記clamped targetに対して1 CSS px以内とする。
7. failure表示、refresh、source availability更新でfocusを移動しない。
8. visible failure railを追加のlive regionにせず、既存の単一announcerから1回だけ通知する。
9. failure payloadからsource、citation、missingAspectsを合成しない。

固定文言とerror code mappingは既存契約を維持し、内部例外本文を表示しない。

## Sources Disclosure Contract

1. wide inline panel、compact sheetとも、新しいmount / hard load時の初期値をclosedとする。
2. source surfaceをopenにする唯一の入口は、source countが1件以上ある状態でのsources trigger明示操作とする。
3. 次のeventで自動openしない。
   - `/research`からの新規thread作成とredirect
   - existing threadへのfollow-up submit
   - queued / running開始
   - source countの0→正数
   - completed / failed RSC refresh（cancelはfailedのerrorCode variant）
   - hydration
   - viewport mode変更
4. source 0件ではtriggerをdisabled、`aria-expanded=false`とし、inline asideもsheetもmountしない。
5. source countが0件から正数へ変わった場合はcountとbutton availabilityだけを更新し、closedを維持する。
6. same-threadのsubmit、live update、terminal、RSC refreshではuser-selected open / closedを維持する。
7. explicit open中にfinal sourceが増えた場合は同じpanel内のlistだけを更新し、panel identity、source scrollTop、
   answer scrollTop、composer input、focusを維持する。
8. closed中にfinal sourceが増えても、answer panelとcomposerの幅を変えない。
9. disclosure ownerはcanonical `threadId`を受け取り、stateをthread ID単位で所有する。同じthread IDのRSCでは
   同一ownerを維持し、thread IDが変わる真のnavigation、A→B→Aのroute cache復帰、hard reloadでは
   commit前からclosedとして描画する。URLやstorageへ永続化しない。
10. `<1280px`ではinline asideを描画せず、sheetはユーザー操作時だけopenする。
11. viewport modeを跨いだ場合は現在のsurfaceをclosedへ収束させ、`>=1280px`へ戻っても自動openしない。
    inline asideとsheetを同時に表示しない。
12. 回答領域を320px縮め、composer spacerを追加するのはwide triggerの明示open時だけとする。
13. triggerの`aria-expanded`は実際のsurface表示と一致させる。surfaceがmountされていないclosed / 0件時は
    `aria-controls`を省略し、open時だけ現在のinline asideまたはsheetのstable IDを指定する。
14. compact sheetのfocus trap、Escape、close後のtrigger focus復帰を維持する。
15. source count更新だけではlive announcementを行わない。

## Missing Aspects Display Contract

`missingAspects` は検索結果やsourceではなく、対応するfinal answerで確認できなかった観点である。
誤認を避けるため、値だけを `/` 連結して表示せず、answer直後に次の意味を持つ補足領域として表示する。

- visible label: `確認できなかった点`
- 1項目ずつ読めるlist semantics
- 対応するassistant answerの内側に留め、sources panelへ移動しない
- source count、search result count、run progressとして扱わない

API fieldと保存値は変更しない。

## Test Ownership

### Backend unit / integration

1. prior assistantと`missing_aspects`を持つ履歴をworkerへ渡し、context preparationとagent inputまで
   正しいsnapshotを利用できる。
2. historyなしの初回質問と、同じthreadのfollow-up質問がともにcompletedになる。
3. repositoryが`role / content / missing_aspects`をseq順で投影する既存testを維持する。
4. 想定外例外はassistant messageを作らず`failed/internal_error`へ収束する既存安全契約を維持する。
5. external LLM、実ユーザーデータ、production secretを使わずfake adapterで検証する。

### Static local topology

1. `docker-compose.yml` の全 `worker-*` serviceと`scheduler`の集合がMakefile `WORKERS`と一致する。
2. `worker-agent` が`pipeline-restart`、`pipeline-down`、default `pipeline-logs`、`migrate-safe`へ展開される。
3. `QUEUES`に`agent`が含まれる。
4. `make -n pipeline-restart`にbackend、全worker、schedulerの`--force-recreate`が現れる。

### Frontend component

1. `visible draft -> SSE failed -> persisted failed rerender`を1つのtestで実行する。
   - draftをterminal commitで除去する。
   - 同じcommitでfailure railを1件表示する。
   - persisted rerender後もturnとfailure railのDOM identityを維持する。
   - focusを維持する。
2. SSEまたはpollingが観測する`status=failed`について、`errorCode=cancelled`を含む各既知variantで同じ
   連続性契約を確認する。enqueue_failedなどlive terminalを持たない経路はpersisted failed fixtureで確認する。
3. failed contractionを末尾から96px / 97pxの両条件で確認する。
4. sourcesが存在するwide viewportでも初期closedである。
5. `0 sources -> final sources`でbuttonだけenabledとなりclosedを維持する。
6. explicit open / closed後、同じthreadのqueued / active / completed / failed rerenderでstateを維持する。
7. sources更新でtextarea、answer scroller、live controller、announcerのidentityを維持する。
8. 1279pxから1280pxへ変更しても自動openせず、inline / sheetを排他的にする。
9. successful existing-thread submitでclient `router.refresh()`を呼ばず、Server Action更新だけを使う。
10. missing aspectsを`確認できなかった点`付きのlistとして描画する。

### Playwright

Playwrightのterminal continuityは実providerや実workerのraceに依存させず、次の決定的fixtureで検証する。

1. 既存`backend/scripts/seed_e2e_research.py`を、過去assistant、`missing_aspects`、sourceを持ち、2件目の
   user runが`running`である固定follow-up threadをseedできるように拡張する。
2. 同scriptへ、固定runだけを条件付きUPDATEで`running -> failed/internal_error`へ遷移させるtest commandを
   追加する。既存production guard、固定UUID allowlist、cleanupを維持し、production endpointは追加しない。
3. `frontend/scripts/run-research-e2e.mjs`はsuite前後のseed / cleanupを所有する。Playwright Node fixture helperは
   terminal同期点で固定transition commandだけを実行する。任意SQLや任意run IDを受け取らず、外部LLMと
   provider keyを使わない。
4. `page.addInitScript()`で対象runのEventSourceだけを制御するfakeを導入し、他runへ影響させない。
   有効なattempt / generation / event IDを持つdraftとfailed terminalを、DB transitionとの同期点を明示して送る。
5. terminal送信前にbrowserへMutationObserverと`requestAnimationFrame` samplerを注入する。stable turn配下の
   draft count、failure count、turn / main / composerのbounding box、answer scrollTopを、persisted failed markerが
   現れるまで各paint boundaryで記録する。計測用attributeは表示契約を持たず、test selectorに限定する。

必須browser scenario:

1. `/research`から固定active threadへnavigationした場合と、そのthreadを1440pxでhard loadした場合の両方で
   inline asideは初期closedとなり、answer panelとcomposerが全幅を維持する。explicit open時だけ320px縮小する。
2. source 0件のfixtureではtriggerがdisabledでsurfaceをmountしない。component testの
   `0 sources -> final sources` rerenderでもclosedを維持する。
3. running follow-up fixtureでdraftを表示し、DBをfailedへ遷移させてからfake SSE terminalを送る。
4. terminalからpersisted RSC commitまで、各sampleでdraftまたは固定failureが常に1件だけ存在し、
   protected loading skeleton、document reload、source auto-openを発生させない。
5. persisted failed commitでturn anchor、clamped answer scrollTop、main panel、composerの位置が定義済み許容差を
   超えて跳躍しない。
6. sourcesを明示openしたvariantでは、同じterminal refresh後もopen、source scrollTop、focusを維持する。
7. compact viewportではsheetを自動openせず、明示open時だけmodal / focus contractを適用する。
8. A→B→A navigationでは各threadをclosedで開始し、route cacheから過去のopen stateを復元しない。
9. console errorがない。

actual POST accepted後にclient `router.refresh()`を呼ばないことは`ResearchComposer` component testの責務とする。
provider依存の実submitを必須Playwright scenarioに含めない。

### Runtime restart probe

長寿命workerのrevision skew自体はpytestのfresh processだけでは再現できないため、local runtimeでは次を確認する。

1. `worker-agent`のcontainer IDと開始時刻を記録する。
2. `make pipeline-restart`を実行する。
3. `worker-agent`のcontainer IDまたは開始時刻が更新され、processがrunningであることを確認する。
4. startup logにFATAL、import error、`ThreadMessageSnapshot` / `missing_aspects` AttributeErrorがないことを確認する。
5. fake worker integrationとtopology testを組み合わせて、fresh revisionのfollow-up処理を決定的に保証する。

dev egressとprovider credentialが利用できる環境では、初回質問とsame-thread follow-upの実completed smokeを
追加で実施してよい。このmanual provider smokeは外部要因で非決定的なため、automated Doneのblocking条件にしない。

## Expected Change Scope

実装時の変更候補:

```text
Makefile
docs/development.md
backend/tests/test_local_runtime_topology.py                 # または同責務の既存test
backend/scripts/seed_e2e_research.py                         # fixed active run / transition / cleanup
frontend/src/features/research/api/submit-research-question.ts
frontend/src/features/research/components/ResearchComposer.tsx
frontend/src/features/research/components/ResearchThreadView.tsx
frontend/src/features/research/components/ResearchThreadLiveBoundary.tsx
frontend/src/features/research/components/ResearchSourcesPanel.tsx
frontend/src/features/research/components/ResearchAnswerSlot.tsx
frontend/src/features/research/components/ResearchThreadView.test.tsx
frontend/src/features/research/components/ResearchWorkspaceSources.test.tsx
frontend/src/features/research/components/ResearchComposer.test.tsx
frontend/e2e/fixtures/research.ts
frontend/e2e/fixtures/research-runtime.ts                   # fixed transition command only
frontend/e2e/research.spec.ts
frontend/scripts/run-research-e2e.mjs
```

実装責務に応じてfile配置は調整できるが、live state、stable turn、sources disclosureのownerを再混在させない。

変更しない領域:

```text
backend/app/schemas/
backend/app/models/
backend/alembic/
backend/app/agent/question_context/ のdomain contract
frontend/src/types/*.gen.ts
frontend/src/components/ui/
認証・認可
```

## Rollout and Recovery

1. 実装前のlocal復旧として、active agent runがないことを確認し`worker-agent`を再生成する。
2. 既存のfailed runは変更せず、必要な質問だけをfresh workerへ再送する。
3. topology test、backend agent test、frontend component testを実行する。
4. `make pipeline-restart`でbackendと全workerをfresh processへ揃える。
5. runtime restart probeで`worker-agent`のcontainer開始時刻更新とstartup成功を確認する。
6. fixed active-run fixtureを使うPlaywrightでsource closed、failed convergence、visual continuityを確認する。
7. provider条件が揃う場合だけmanual初回質問 / same-thread follow-up smokeを追加実施する。
8. `/check`を完走する。

rollback時もDB rowやAPI contractは戻さない。frontend変更だけを戻す場合でも、worker process coherence修正と
fresh workerへの再生成は維持する。

## Verification

実装時は次を行う。

1. backend targeted test:
   - local runtime topology
   - agent worker integration
   - thread snapshot projection
2. frontend targeted test:
   - `ResearchThreadView`
   - `ResearchWorkspaceSources`
   - `ResearchComposer`
3. Playwright Research continuity scenario。
4. runtime restart probe。
5. `/check`によるlint、format、types、tests。
6. `git diff --check`。
7. OpenAPI、generated TypeScript types、Alembic headに差分がないこと。

必須fixtureは外部providerを使わない。検証環境のDB、Redis、browserが利用できず必須項目を実行できない場合は、
未実行理由を明記し、別のtest通過で完了扱いにしない。provider credential不足によるmanual smokeの未実行は
automated Doneを妨げない。
