# アプリ共通ローディングフィードバック仕様

> 作成日: 2026-07-22
>
> 更新日: 2026-07-24（実装・自動検証・独立review完了）
>
> Status: Implemented / Automated verification complete
>
> 対象: frontend の初回表示、保護画面のページ遷移、部分更新、mutation pending UI、
> Research 送信・回答確定時の表示連続性

## 位置付け

本仕様は、待機時間そのものを短縮する性能改善ではなく、待機中に「操作が受理されたか分からない」
状態をなくすための UI 契約である。backend API の warm 維持、DB latency、cache hit率の改善とは
独立して成立させる。fallbackが可視化できるのはserverから最初のHTML shellを受信した後であり、
TTFB以前の待機を短縮・可視化するものではない。

`frontend/specs/research-thread-navigation-pending.md` が別仕様として残していた「Research 以外の
loading UI 全面設計」と、`(protected)/loading.tsx` の news 固有 fallback 問題を本仕様で扱う。
Research 内の thread / new / more 切替は同仕様を正本とし、本仕様で state model や lock 契約を
上書きしない。ただし同仕様の「direct loadでは親`(protected)/loading.tsx`を使う」という暫定判断だけは、
本仕様のRoute Ownershipで置き換える。

Research run の SSE / polling / finalization state は
`frontend/specs/agent-research-live-ui-slice.md`、draft の Markdown 解釈とセキュリティ契約は
`frontend/specs/research-live-draft-markdown.md`を正本とする。本仕様はそれらのprotocolや意味を変えず、
送信後のthread遷移、Server Component再取得、draft更新、確定回答置換をまたいで画面が空白化・全面置換されたように
見えないための表示境界とDOM連続性だけを所有する。

## Work Definition

### Problem

現状は、待機状態の責務が root `Suspense`、route `loading.tsx`、局所 `Suspense`、link 横の
spinner、mutation button に分散している。その結果、次の5種類の問題が同時に存在する。

1. 初回表示や一部遷移で可視 fallback がなく、画面が空または無反応に見える。
2. 全 protected route に news 一覧用 skeleton が適用され、遷移先の構造・意味と一致しない。
3. 12px の link 横 spinner や control の disabled だけでは、画面全体が切替中だと気づきにくい。
4. Research の新規質問送信では、Server Actionの`revalidatePath`と
   `/research/[threadId]`への`redirect()`がrootからのRSC patchを発生させ、Research page subtreeが
   置換されるため、送信受付後にworkspace全体が一瞬消えたように見える経路がある。
5. Research の回答確定では、DB正本へ収束するRSC再取得とMarkdownの再描画が同時に起こり得る。
   stable answer slotの契約がDOM / paintとして固定されていないため、通常完了時に画面全体または回答領域が
   「バチっ」と切り替わるregressionを検出できない。

### Evidence

| 対象 | 現状 |
|---|---|
| root | `app/layout.tsx` の最上位 `Suspense` は fallback 未指定。内側の `NonceThemeProvider` が `headers()` を待つ |
| rendering config | `next.config.js` は`cacheComponents: true`。manual `Suspense` fallbackはPPR static shell / RSCへ含まれ得る |
| production build | `index`、`research`、`trends`、`auth/login` の初期 HTML body に可視 UI がなく、pending template だけがある |
| protected gate | `(protected)/layout.tsx` は JSX を返す前に `requireSession()` を待つ |
| protected fallback | `(protected)/loading.tsx` は dashboard masthead と記事一覧 skeleton に固定 |
| news detail | article と watchlist を await してから主画面を返し、主記事用 fallback がない |
| Research entry | thread data を await してから workspace を返し、初回進入用 fallback がない |
| Research internal | 旧本文、対象別文言、overlay、live region、operation lock が実装済み |
| Research new submit | 変更前の`submitResearchQuestion`は`/research`と対象threadを`revalidatePath`し、新規時は`/research/[threadId]`へ`redirect()`していた。Next.js 16.2.11のServer Action reducerはredirect / revalidation responseのFlight dataをrootから適用するため、client側でredirect errorをcatchしてもsubtree置換を防げない |
| Research read cache | thread一覧・詳細・run取得はすべて`cache: "no-store"`であり、新規thread固有URLへのnavigationと既存threadの`router.refresh()`でDB正本へ収束できる |
| Research existing submit | 変更前はclient側に明示的な`router.refresh()`がなく、Server Actionの`revalidatePath`に現在pathのRSC更新を委ねていた。Server Action由来のroot patchではなく、accepted結果後の通常`router.refresh()`を表示境界内で行う必要がある |
| Research finalization | accepted terminalまたはpolling completed後、controllerは`router.refresh()`を行い、確定回答が反映されるまで2 / 4 / 8 / 最大10秒で再試行する。これはdocument reloadではなくRSC payloadのmergeだが、通常completed経路のDOM / paint連続性E2Eがない |
| Research Markdown draft | SSE deltaごとの描画でMarkdown構成と`components` mappingを再生成し、mapping内の新しいcomponent functionがReact element typeになる。parser時間が小さくても、未変更blockの再mountと確定時の視覚的な切替を増幅し得る |
| desktop nav | `ShellNav` / `Header` は12pxの `NavPendingDot`、Dashboard 上段 nav は pending 表示なし |
| mobile nav | link activation 直後に sheet を閉じるが、その後も残る pending 表示がない |
| partial update | dashboard / watchlist の記事領域には形状 skeleton があるが、一部 slot は `null` または空要素 |
| news 404 | `use cache`関数からthrowした`ApiError`はCache environment後にclass identityを保たず、callerの`instanceof ApiError`ではnot-found分類できない。404 / 410はcache内で`null`へ変換し、page / metadataが値として処理する |
| mutation | Login は明示的。Research delete、compact sign-out、retry などは弱い、または途中で idle に戻る |

調査上の判定は、待ち時間の長さにはbackend warm状態やcacheが影響し得る一方、送信・確定時の
full-page blankと急な置換の主因はfrontendのroute ownership、RSC更新境界、React element identityにある、
というものである。したがってFly設定やcache調整で本契約の代替はできず、逆に本仕様もAPI latency自体の
短縮を保証しない。live draft Markdown化は送信・finalization refreshを新しく発生させた原因ではないが、
SSEごとのsubtree再mountにより既存の切替を目立たせる増幅要因として扱う。

Next.js 16.2.11 の仕様と導入済みruntimeでは、同 segment の `loading.tsx` は `layout.tsx` の内側で page と
children を wrapするため、layout 自身の runtime access / uncached access は覆わない。また、
`useLinkStatus` は subtle な link-local feedback であり、route-level fallback と prefetch の補助として
位置付けられている。`router.refresh()`は新しいRSC payloadを取得して既存treeへmergeし、影響を受けない
Client Componentやbrowser stateを保持する契約であるため、Research完了時の全面的なblankは必須挙動ではない。
ReactのTransition / Suspenseも、すでにrevealされた内容を不用意にfallbackへ隠さない境界設計を前提とする。

一次情報:

- [Next.js Fetching Data](https://nextjs.org/docs/app/getting-started/fetching-data)
- [Next.js Linking and Navigating](https://nextjs.org/docs/app/getting-started/linking-and-navigating)
- [Next.js Link Component](https://nextjs.org/docs/app/api-reference/components/link)
- [Next.js useRouter](https://nextjs.org/docs/app/api-reference/functions/use-router)
- [Next.js revalidatePath](https://nextjs.org/docs/app/api-reference/functions/revalidatePath)
- [Next.js useLinkStatus](https://nextjs.org/docs/app/api-reference/functions/use-link-status)
- [Next.js Prefetching](https://nextjs.org/docs/app/guides/prefetching)
- [Next.js Caching / Cache Components](https://nextjs.org/docs/app/getting-started/caching)
- [Next.js `use cache`](https://nextjs.org/docs/app/api-reference/directives/use-cache)
- [Next.js `distDir`](https://nextjs.org/docs/app/api-reference/config/next-config-js/distDir)
- [React Suspense](https://react.dev/reference/react/Suspense)
- [React startTransition](https://react.dev/reference/react/startTransition)
- [Vercel Web Interface Guidelines](https://github.com/vercel-labs/web-interface-guidelines)

### Invariants

1. request-specific nonce と CSP を弱めず、`NonceThemeProvider` の nonce 契約を維持する。
2. `requireSession()` / `requireAdmin()` を迂回せず、protected data fetch より前の fail-closed gateを維持する。
3. loading UI に user data、記事タイトル、Research 内容など認可前の情報を含めない。
4. client navigation 中は、可能な限り現在の画面を保持し、空白へ置換しない。
5. shell navigation は pending 中も利用でき、別の行き先へ訂正できる。
6. `<Link>`のanchor / history / scroll semantics、Cmd/Ctrl/Shift click、middle click、外部 link を維持する。
7. Research 内切替の first-wins、旧本文保持、operation lock、overlay、live region を維持する。
8. loading indicator の出現・消滅で主要レイアウトを移動させない。
9. 色またはanimationだけに依存せず、可視文言と支援技術向けstatusを持つ。
10. `prefers-reduced-motion` では pulse / spin / fade animation を停止できる。
11. loading完了を遅らせる minimum display time や人工的delayをproductionへ追加しない。
12. 同じ待機状態を複数箇所から独立管理せず、責務ごとに単一の pending sourceを持つ。
13. Researchの新規質問送信では、受付から遷移先threadのcommitまでpersistent shellと現在のworkspaceを
    空白へ置換せず、送信中であることをworkspace内の明示的なstatusで示す。
14. Researchの回答確定では、masthead、workspace frame、composer、message scroller、対象answer slotを
    再取得の前後で安定させ、表示中draftとDB確定回答を同じslot内で原子的に置換する。
15. Research Markdown rendererのcomponent typeとplugin / option identityは、意味のある設定値が変わらない限り
    SSE delta間で安定させる。parserの処理時間だけを根拠に再mountコストを無視しない。
16. Researchの送信・確定待ちをglobal page navigationへ二重登録せず、既存のrun state、operation lock、
    live region、focus、auto-follow契約を維持する。
17. Researchのsubmission、内部navigation、thread削除はfeature-localな単一operation coordinatorで
    first-winsにし、いずれかのpending中に別operationを開始しない。
18. accepted submissionは、対象threadの対象runがqueued / runningまたはterminalのいずれで最初に
    page modelへ現れても、exact `threadId + runId`のcommitでpendingを終了する。

### Non-goals

- backend API の `min_machines_running`、startup、timeout、DB、Redis、cache 設定の変更。
- latency、TTFB、cache hit率、Web Vitalsなどobservability / metricsの追加。
- serverが最初のresponse byteを返すまでの空白時間の解消。
- API response、Pydantic schema、生成TypeScript型、DB schemaの変更。
- 認証・認可ポリシー、session lifetime、cookie cacheの変更。
- nonce方式CSPの撤去、CSP directiveの緩和、PPRとの全面的な再設計。
- 全操作を塞ぐglobal modal spinner。
- skeletonを実データ件数や文章量と完全一致させること。
- browser back / forward、外部遷移、downloadを含む全navigationのpending捕捉。
- Next.js公開APIで観測できないsilent navigation cancelと、現在URLのlinkによるpending cancel。
- Link pendingが一度も発生せず、redirect / refresh後のcommit URLもoriginと同一になる場合のMobile Sheet自動close。
  global pendingは開始されないためstuckせず、Sheetはuserが閉じられる状態を維持する。
- production browserでのprefetch request timing / hit率の自動計測。static shell / RSC artifactの確認までは含む。
- 新規dependencyまたはglobal state libraryの追加。
- Research live answerのevent vocabulary、polling / SSE接続、run state遷移、finalization retry間隔の変更。
  それらを使った送信・確定時の表示連続性は本仕様の対象とする。

### Done

- serverから最初のHTML shellを受信した後のfirst paintで、neutralな可視fallbackが表示される。
- desktop / mobile のsame-tab page navigationが100msを超えてpendingの場合、行き先を示す共通statusが見える。
- client navigation中は旧画面を保持し、切替対象領域にoverlayを表示する。
- route shellへ到達後に待機が続く場合、遷移先に対応したskeletonまたはfeature-local fallbackを表示する。
- news detail、Briefing、Trends、Watchlist、Research、Adminでnews固有fallbackを誤表示しない。
- mutationはcontrol-localなspinnerまたはpending動詞、もしくはoptimisticな即時変化を持つ。Research thread削除は
  destructive actionとしてspinnerと`削除中…`の両方を持つ。
- Researchの新規質問送信では、送信受付から遷移先threadのcommitまで現在workspaceと明示的な受付statusを
  保持し、full-page blank、無関係なroute fallback、操作受付が不明な状態を発生させない。
- Researchの通常completed経路では、`回答を確定しています…`と表示中draftを維持したままRSC再取得を行い、
  shell / composer / scroller / answer slotを消さずにDB確定回答へ置き換える。
- SSE draft更新でMarkdown subtreeを設定identityの変化により再mountせず、確定回答置換を含むDOM連続性を
  component testと認証済みE2Eで固定する。
- visible status、`aria-busy`、`aria-live`、reduced motionの契約がcomponent / E2E testで固定される。
- production buildのstatic shell / fallback subtreeと、2秒遅延を使うdesktop / mobile E2Eで上記を確認できる。

## UX Model

待機状態を次の6種類に分ける。見た目が似ていても、所有者と終了条件を混ぜない。

| 状態 | 開始 | 終了 | 主表示 |
|---|---|---|---|
| `app_bootstrap_pending` | 初回documentでroot dynamic boundaryがsuspend | root childrenがstreamされる | neutral bootstrap fallback |
| `page_navigation_pending(target)` | initiating Linkの`useLinkStatus().pending=true` | 同Linkのpending終了、origin以外のURL commit、committed error / not-found mount、provider unmount | page status band + 旧画面overlay |
| `content_pending(scope)` | 現在route内のdata / search params更新 | 対象contentの解決またはerror | 画面形状に合うskeleton |
| `mutation_pending(action)` | server action / mutation受付 | success redirect / resultまたはerror | button内spinner + pending動詞 |
| `research_submission_pending` | Research composerが有効な質問をsubmit | 遷移先threadまたは同threadのactive run表示がcommit、もしくはinline error | 現在workspace + `質問を送信しています…` status |
| `research_finalization_pending` | accepted completed terminalまたはpolling completed | 同じrunのDB確定assistant messageが同じanswer slotへcommit、または終端errorへ収束 | 表示中draft + `回答を確定しています…` |

表示の基本シーケンスは次とする。

```text
初回表示
  neutral bootstrap fallback
    -> 認可済みroute shell + 画面固有skeleton
      -> 実content

client page navigation
  旧画面 + page status band + content overlay
    -> 遷移先shell + 画面固有skeleton（追加待機がある場合）
      -> 実content

部分更新
  現在shell + 対象領域skeleton / busy status
    -> 更新content

mutation
  現在画面 + button内「保存中…」等
    -> success / redirect / inline error

Research新規質問
  現在workspace + composer pending + workspace内「質問を送信しています…」
    -> accepted結果 + persistent Research shell内のclient navigation
      -> 遷移先threadのactive run
      -> live draft

Research回答確定
  同じanswer slotのdraft +「回答を確定しています…」
    -> RSC merge
      -> 同じanswer slotのDB確定回答
```

## Visual Contract

### 1. App bootstrap fallback

root `Suspense` は、fallback省略ではなくneutralな `AppBootstrapLoading` を持つ。

- `VECTOR` の識別要素、spinner、`画面を準備しています…`を表示する。
- request header、session、theme client stateを読まないServer Componentとする。
- script、inline event、user dataを含めず、nonce解決前でも安全にrenderできる。
- light / darkのどちらでも判読できるsystem colorまたはstatic tokenを使う。
- bodyの中央に小さなspinnerだけを置くのではなく、最低限のsurfaceとstatus文言を持つ。
- first paintからsurfaceとstatus文言を可視にし、opacity 0から始まるappearance delayは使わない。
- minimum display timeは設けず、childrenが解決したら即座に置換する。
- production buildで生成されるstatic shellまたはfirst streamed fallback chunkにstatus文言が含まれることを
  必須とする。

このfallbackはnonce取得とprotected gateの両方を安全側から覆う。認証判定をroot外へ移したり、
認証前にprotected shellを描画する理由には使わない。

### 2. Page navigation status

same-tabの主要page navigationでは、link横のdotだけでなく、viewport内の共通位置に
`PageNavigationStatus` を表示する。

- persistent mastheadがある画面ではmasthead直下、その他はsafe areaを考慮したviewport上端に置く。
- scroll位置に関係なく、desktop / mobileの現在viewportから見える。
- mobile navigation起点ではSheetをpending終了まで開いたままにし、Sheet内の共通位置へ大きなstatusを表示する。
  この間はSheet内statusを唯一のvisible / live statusとし、背面のglobal bandを重複通知しない。
- overlay layerとして表示し、出現でdocument flowを押し下げない。
- spinner、行き先別文言、十分なcontrast、borderまたはsurfaceを併用する。
- spinnerはdecorativeとして`aria-hidden=true`とし、意味は文言とlive regionが持つ。
- shell navigationを覆ったり、画面全体のpointer eventを無効化しない。
- link横の`NavPendingDot`は補助表示として残せるが、共通statusの代替にはしない。

表示文言はnavigation targetのproduct labelから決める。

| Target | 文言 |
|---|---|
| `/` | `ニュースを読み込み中…` |
| `/news/[id]` | `記事を読み込み中…` |
| `/research*`への外部feature遷移 | `Researchを読み込み中…` |
| `/briefing`、`/briefing/[category]` | `Briefingを読み込み中…` |
| `/trends` | `トレンドを読み込み中…` |
| `/watchlist` | `ウォッチリストを読み込み中…` |
| `/settings` | `Settingsを読み込み中…` |
| `/admin/pipeline-status` | `Pipeline Statusを読み込み中…` |
| `/admin/source-health` | `Source Healthを読み込み中…` |
| 未分類の内部route | `画面を読み込み中…` |

記事タイトルなど長い可変文字列をglobal statusへ含めない。Research 内thread切替だけは既存仕様に従い、
対象thread名をfeature-local statusへ含める。

### 3. Current content overlay

client page navigation中は現在の画面を残し、切り替わる主要content領域に薄いoverlayを重ねる。

- overlayは旧内容を完全に隠さず、「現在見えている内容は遷移前」と分かる濃度にする。
- status bandと同じpending targetから表示を導出し、別のbooleanを持たない。
- shell masthead、theme、sign-out、主要navigationはoverlayの外に置く。
- 全画面modal、中央の巨大spinner、backgroundの完全なwhite-outは使わない。
- global overlayはvisual feedbackを担当し、操作lockは追加しない。
- stale mutationを止める必要があるfeatureは、Researchのようにfeature仕様でlock範囲を定義する。

### 4. Route / content skeleton

skeletonは「データが入る場所」を示す補助であり、page navigationの受付表示を単独で担わない。

- persistent shell、masthead、page titleなどdata非依存部分は実DOMを先に表示する。
- skeletonは遷移先の最初のviewportの主要構造を写す。
- card件数や可変章数を実結果と完全一致させる必要はないが、解決時に大きなlayout jumpを起こさない。
- paper画面ではglobal `bg-accent`をそのまま使わず、paper surface上で識別できる専用contrastを使う。
- skeleton群は`aria-hidden=true`とし、領域ごとに1つだけvisible status / live statusを置く。
- `animate-pulse` / spinnerは`motion-reduce:animate-none`相当を持つ。
- status文言は`…`を使い、`...`は使わない。

### 5. Mutation pending

通常のmutationはpage statusへ昇格させず、操作したcontrolの中で完結させる。Research質問送信のように
受付後もroute commitや長時間runへ続く操作は、次節のfeature-local statusを併用する。

- optimistic updateで意味のある状態が即時反映される場合、spinnerを必須にしない。
- optimistic updateがない場合、control内のspinnerまたはpending動詞の少なくとも一方を表示する。
- destructive actionや長引くことが想定されるactionは、先頭iconをspinnerへ置換し、labelもpending動詞へ変える。
- `保存` -> `保存中…`、`削除` -> `削除中…`を基準とする。retryは完了signalを持つ場合だけ`再試行中…`を使う。
- pending中はnative `disabled`で二重submitを防ぎ、pendingを所有するbuttonまたはform / regionへ
  `aria-busy=true`を付ける。
- icon-only controlはspinnerとbusy semanticsを持ち、長引く処理ではaccessible nameの更新または可視statusを
  近接領域へ出す。
- promise / transitionなどの完了signalを持たないretryでは、固定時間を実処理のpending stateとして扱わない。
  `再試行を開始しました`という受付feedbackと、error boundaryが所有する実際の再描画を分ける。

### 6. Research submit / finalization continuity

Researchの質問送信と回答確定は、button横の小さいspinnerやRSC mergeだけに状態説明を委ねない。

- submit開始時はcomposerを`aria-busy=true`にし、buttonを`送信中…`へ変える。同じstateからworkspaceの
  主content内にもspinnerと`質問を送信しています…`を含む明示的なstatus panelを表示する。
- status panelはviewport内で認識できるanswer領域またはempty view内に置き、buttonの近くだけに限定しない。
  既存thread本文は読める状態で残し、全画面modalや内容を隠す不透明overlayにはしない。
- 新規threadへのclient navigation中もpersistent Research shellを維持し、protected共通fallback、
  `ResearchWorkspaceSkeleton`、空白へ戻さない。workspace skeletonはResearchへの初回進入だけに使う。
- submit成功後は遷移先または同threadで対象runがpage modelへcommitした時点でsubmission statusを終了する。
  最初のmodelですでにcompleted / failed / policy_blockedでもexact `threadId + runId`が一致すれば終了し、
  別threadまたは別runでは終了しない。action error時はstatusを外し、既存inline / toast errorと
  再操作可能なcomposerへ戻す。
- Server Actionは認証・入力検証・API mutationとaccepted / daily-limit結果の返却を所有し、
  `redirect()` / `revalidatePath`を行わない。新規threadはaccepted runのUUIDをschema検証して得たexact
  `/research/[threadId]`へpersistent submission boundaryから`router.replace()`し、既存threadは
  `router.refresh()`する。どちらも`no-store`のpage modelでDB正本へ収束する。
- completed検知後は既存の`finalizing` stateを唯一のsourceとし、表示中draftと
  `回答を確定しています…`を同じanswer slotに残す。global page bandやroute skeletonを追加表示しない。
- RSC payloadのcommit時は、同じrunのdraft / finalizing表示とDB確定answerを同じanswer slot内で原子的に
  入れ替える。draftとfinalを同時表示せず、その間にzero-height / blank frameを挟まない。
- `PaperSurface`、`ShellMasthead`、workspace frame、composerは新規thread navigationとfinalization refreshの
  外側に安定して残す。既存threadのmessage scrollerはsubmit / refresh、対象answer slotはfinalization refreshを
  またいで維持する。データ内容は更新してよいが、これらのDOM identityを不要に変更しない。
- Markdown rendererの共有構成は、設定値が同じ間はcomponent function、plugin array、
  `remarkRehypeOptions`を安定させる。draft textの変化だけで既存Markdown elementを別typeとして再mountしない。
- これはstream全体のblock memoizationやthrottleを必須化するものではない。設定identityの安定化と
  未変更DOMの再利用を、parser benchmarkとは別のcorrectness / continuity契約として追加する。
- DOM identityの保証は、parse / serialize時間のbenchmarkで代替しない。React rerender時のnode identityと、
  browser上のanimation frameごとの可視領域を別々に検証する。

## Navigation State Contract

Research外のpage navigationは、`(protected)`で持続するclient providerが次を所有する。

```text
pendingNavigation = {
  sourceKey,               # mounted Link observerを識別するclient-localなopaque key
  originHref,              # 開始時にcommit済みだったURL
  targetHref,              # pathname + 意味のあるsearch paramsを正規化したURL
  label
}
```

状態遷移は、Next.jsの公開APIで観測できるeventだけで定義する。

```text
idle
  -> linkPending(A, true)                 -> pending(A)

pending(A)
  -> linkPending(B, true)                 -> pending(B)     # 表示はlatest-wins
  -> linkPending(A, false)                -> idle           # redirect / cancelを含むhistory settle
  -> committedHref != originHref          -> idle
  -> committed error/not-found UI mount   -> idle
  -> provider unmount                     -> state破棄

pending(B)
  -> linkPending(A, false)                -> pending(B)     # stale targetの終了は無視
  -> linkPending(B, false)                -> idle
  -> committedHref != originHref          -> idle           # targetまたはredirect先のcommit
  -> committed error/not-found UI mount   -> idle
  -> provider unmount                     -> state破棄
```

- 対象linkは`<Link>`のdefault SPA navigation、href、history、scroll属性を維持し、global feedbackのために
  `preventDefault()`やmanual `router.push`へ置き換えない。
- modifier click、middle click、`target="_blank"`、external URL、downloadではglobal stateを開始せず、
  browser defaultを維持する。
- shell navigationはlockしない。pending中に別pageを選び、そのLinkのpendingがtrueになるとtarget表示をlatestへ
  置換し、Next.jsのdefault routerに先行navigationの破棄を任せる。
- 各`PendingAwareLink`は子の非表示`LinkPendingObserver`で`useLinkStatus().pending`をproviderへ通知する。
  observerはmount中に安定する`sourceKey`を生成し、`true`を観測した後のfalling edgeだけを同じkey付きで通知する。
  providerはhrefではなくcurrent `sourceKey`の終了だけを受理するため、initial `false`、同hrefの別Link、stale Aの
  `false`でpending Bを解除しない。
- `useLinkStatus`の値をlink横だけのprimary feedbackとして直接見せず、same-URL redirect / router cancelを含む
  initiating Linkのlifecycle sourceとしてproviderへ集約する。可視band / overlayはprovider stateから描画する。
- React `startTransition` / `useTransition().isPending`と`router.push`の戻り値はroute完了signalに使わない。
  公開APIは個別navigationに紐づくsettle / cancel IDを提供しないためである。
- providerは`usePathname` / `useSearchParams`でcommit済みURLを観測する。pending開始後にorigin以外のURLが
  commitされたら、exact target、redirect先、not-found targetのいずれでもpendingを解除する。
- 実際にcommitされたrouteのerror / not-found UIにはclient notifierを置き、mount時にcurrent pendingを解除する。
  unmountされた旧treeや未commitのresponseからresetを通知しない。
- MobileNavはnavigation pending中もSheetとinitiating Linkをmountしたまま保ち、Sheet上部にtarget文言を表示する。
  Sheet内の別linkは利用可能でlatest targetへ訂正できる。current Linkのpending終了、URL commit、error / not-foundで
  Sheetをcloseする。navigation settleによるcloseでは`onCloseAutoFocus`のdefaultを抑止し、commit先に対するNext.jsの
  focus処理をmenu triggerへ戻さない。Escape、outside click、明示的closeでは通常どおりtriggerへfocusを戻すため、
  close reasonを区別する。Radixのclosed modal subtreeをforce-mountしてobserverを保持する実装は使わない。
- prefetchによりLink pendingが一度も`true`にならず、最終commit URLもoriginと同一の場合は公開signalだけでsettleを
  判定できない。このedgeでは推測timerでcloseせず、Sheetを操作可能なまま残してuserの明示closeに委ねる。
- Aの後にBを選びAのresponseを先に解決してもAがcommitされないことは、Next.jsのrouter behaviorとしてE2Eで
  固定する。`sourceKey`はmounted observerのidentityでありnavigation試行を表すoperation IDではない。
  Next.jsの公開APIに存在しないrouter operation IDや完了推測timerは追加しない。
- prefetched routeでLink pendingがskipされた場合はglobal state自体を開始しない。commitが即時なら実content、
  manual boundaryがcommitされるなら画面固有statusがfeedbackを担当するため、global bandを人工的に点灯しない。
- genericなcurrent URL linkはNext.jsのsame-page refresh semanticsを維持し、Link pendingが発生すればglobal
  feedbackも表示する。先行navigationをcancelする専用UI契約には使わない。
- pathnameに加えてsearch paramsがdestination identityに必要なlinkは、正規化したhrefで比較する。
- programmatic navigationは、既にcontent pendingまたはmutation pendingを持つ経路ではglobal stateへ二重登録しない。

Research 内navigationは既存のfirst-wins contractを維持し、このlatest-wins stateへ統合しない。
Research外から`/research*`へ入るnavigationと、Researchから別featureへ出るnavigationだけがglobal targetを使う。
`/research*`表示中のdesktop / mobile ShellNavのResearch itemはsection-currentなno-opとし、`/research`への
global navigationを開始しない。新しいResearch workspaceは既存のfeature-local `new` controlだけから開始する。
これによりShellNav clickがthread -> newをglobal latest-winsとして迂回する経路を作らない。

## Route Ownership

protected共通の `(protected)/loading.tsx` は削除する。route-neutralな形へ変えるだけでは、client transition中に
旧画面を共通fallbackへ置換し得るためである。既存のAdmin配下3つのroute `loading.tsx`も削除し、同じ形状を
page内のmanual `Suspense` fallbackへ一本化する。本仕様の対象protected routeには新しい`loading.tsx`を追加しない。
共通fallbackの削除と、全対象routeのfeature-local fallback追加は同じsliceで行い、中間状態で初回表示を
blankへ退行させない。

初回の認証・nonce待機はroot bootstrap fallback、認可後の画面待機はdata accessに近いfeature-localな
manual `Suspense` boundaryが担当する。client transitionで旧画面を保持できることは推測せず、2秒遅延probeで
境界ごとに確認する。
fallbackを出すために独立fetchを直列化しない。認証・認可gateの解決後、互いに独立したrequestは並行開始し、
awaitだけを対象boundary内のasync childへ遅らせる。共有する結果は同じpromise / request memoizationを使い、
同一dataをfallback用とcontent用に二重取得しない。

本repoは`cacheComponents: true`であり、manual `Suspense` fallbackもstatic shell / client navigation用RSCへ
含まれ、prefetchされ得る。`loading.tsx`撤去で同file固有のroute boundaryは失うが、dynamic routeのprefetch全体が
無効になるとはみなさない。`<Link>`のdefault prefetchを維持し、`prefetch=true`による全dynamic contentの強制取得や
`prefetch=false`の一括設定は追加しない。manual fallbackを含むprefetch可能なRSC shellはproduction build artifactで
確認し、実trafficでのhit / timingは別performance仕様で扱う。

| Route / Scope | 共通page status | content fallback | 決定 |
|---|---|---|---|
| `/` 初回 | bootstrap | dashboard masthead + article grid | categories / articlesの待機をmanual boundaryへ分離する |
| dashboard category / sort / pagination | 使わない | `DashboardArticleListSkeleton` + visible `記事を更新中…` | 現行content pendingを強化する |
| `/news/[id]` | `記事を読み込み中…` | `NewsDetailSkeleton`、関連記事は別fallback | data非依存detail shellを先に返す |
| `/briefing` | `Briefingを読み込み中…` | 現行`BriefingListSkeleton` | mobile wrap、contrast、statusを修正する |
| `/briefing/[category]` | `Briefingを読み込み中…` | 現行`BriefingDetailSkeleton` | 認可後にdata非依存detail shellを先に返す |
| `/trends` | `トレンドを読み込み中…` | 現行`TrendsContentSkeleton` | 可変カテゴリ、contrast、statusを修正する |
| `/watchlist` | `ウォッチリストを読み込み中…` | article grid skeleton | search params待機をmanual boundary内へ移し、空slotを形で保持する |
| `/research*`への進入 | `Researchを読み込み中…` | `ResearchWorkspaceSkeleton` | private情報なしでworkspaceのmasthead / sidebar / composer / detail railを表す |
| Research内thread / new / more | global表示なし | 既存の旧本文 + overlay | 既存Research仕様をそのまま維持する |
| Research質問送信 | global表示なし | 現在workspace + `質問を送信しています…` | composerだけでなく主content内にも受付を表示し、新規thread navigationをpersistent shell内でcommitする |
| Research回答確定 | global表示なし | 同じanswer slotのdraft + `回答を確定しています…` | `router.refresh()`の前後でslotを保持し、DB確定answerへ原子的に置換する |
| `/settings` / `/admin/*` | target固有の上記文言 | 各page内のadmin skeleton | route loadingを削除し、in-page skeletonへ一本化する |
| auth login mutation | 使わない | button内`ログイン中…` | 現行契約を維持する |

### Skeleton shape contract

最初のviewportで、実contentの主要block、column、control位置が対応することを必須とする。

| Fallback | 必須構造 | 完全一致を求めないもの |
|---|---|---|
| Dashboard / Watchlist article grid | responsiveな1 / 2列、source行、複数行title / summary、watch action、viewport + 1行を覆うcard数 | 最終総件数、実title文字数 |
| News detail | back導線、headline、metadata、action、本文column、関連article rail | 本文段落数、関連記事の最終件数 |
| Briefing list | mobileでwrapするmasthead、summary、band cardの見出し / metadata / body | ready / pending categoryの最終内訳 |
| Briefing detail | document header、timeline spine / marker、章見出し / 本文、注目点、重要記事、disclaimer | 可変章数と文章量 |
| Trends | intro / metadata、category section見出し、ranked item row | 記事title、最終category別件数 |
| Research | workspace header、sidebar row、detail rail、composer frame | thread title、thread件数、user固有state |
| Settings / Admin | page title / description、主要control、summary cardまたは実tableと同じcolumn rhythm | rowの最終件数、実status値 |

genericな同じ長方形の反復だけで済ませず、実DOMとfallbackで同じcontainer幅 / grid / breakpoint規則を使う。
実dataをfallbackへ渡して形を合わせることは禁止し、認可後でもplaceholderはprivate contentを含めない。

Researchはpersistentな `research/layout.tsx` に`PaperSurface`、`ShellMasthead`、workspace frameとmanual
`Suspense` boundaryを置き、初回進入だけ`ResearchWorkspaceSkeleton`を表示する。
`/research/page.tsx`と`/research/[threadId]/page.tsx`で同じshellを重複所有しない。一度revealされた同boundaryは
thread / new / more切替、新規質問navigation、finalization refreshでfallbackへ戻さず、内側の
`ResearchNavigationBoundary`が旧本文、submission status、operation lock、finalizing answer slotを保持する。
route `research/loading.tsx`は追加しない。`/research/[threadId]`のfallbackにthread title、件数、user情報は含めない。

layoutへ移すのはroute間で同じ責務とidentityを持つshellだけとし、thread dataやrun stateをlayoutへ
二重管理しない。Server Componentのpage modelが更新されても、persistentなclient境界は現在表示を保持して
新modelをcommitできる所有関係にする。not-found / errorは明示的なrejected outcomeとしてcommitし、
retained workspaceまたは初回skeletonと失敗UIを同時表示しない。direct loadではrejected markerを
server markupへ含め、compiled CSSの`:has()`でhydration前からinitial / retained領域を非表示にする。
client stateはhydration後のretained → rejected遷移を引き継ぎ、inline script / styleは追加しない。実装方式は、
`/research`と`/research/[threadId]`を同じpathnameへ
偽装せず、accepted結果から正規URLへ遷移する。`redirect()` / `revalidatePath`を外すのはDB正本への収束を
省略するためではなく、Next.jsのServer Action root patchを避けるためであり、`no-store` readを伴う
`router.replace()` / `router.refresh()`を収束条件とする。

### Global overlay mount point

providerはtarget、status band、transition lifecycleだけを所有する。主要contentへoverlayと`aria-busy`を付ける
`PageNavigationContent`は、masthead / navの内側ではなく次のcontent outletに配置し、同じprovider stateを読む。

| 画面群 | `PageNavigationContent` の所有者 | overlay外に残すもの |
|---|---|---|
| Dashboard `/` | page内の`<main>` wrapper | `DashboardMasthead`とそのnavigation |
| `/briefing` / `/trends` | `(shell)/(main)/layout.tsx` の`children` wrapper | `ShellMasthead` |
| `/watchlist` | page内の`<main>` wrapper | page内`ShellMasthead` |
| `/news/[id]` / `/briefing/[category]` | 各detail pageの`<main>` wrapper | page内`ShellMasthead` |
| `/settings` / `/admin/*` | `(shell)/(admin)/layout.tsx` の`children` wrapper | `Header` |
| `/research*` | persistent `research/layout.tsx` のworkspace outlet | `ShellMasthead`、Research内外へ離脱できるshell navigation |

`PageNavigationContent`はstateを持たず、global pending時だけ`aria-busy=true`とpointer-eventを奪わないvisual
overlayを描画する。Research内部pending時はglobal stateを開始しないため、Research固有overlayと二重表示しない。

## Navigation Coverage

「主要navigation」は認証済み画面から同一tabで別page / pathnameへ移る内部導線とし、次を対象にする。

| 分類 | 対象 |
|---|---|
| global nav | `ShellNav`、`Header`、`DashboardMasthead`、`MobileNav`の `/`、Research、Briefing、Trends、Watchlist、Settings。ただしResearch表示中のResearch itemはno-op |
| home | `SlimMasthead` / `Header` / Dashboard wordmark、Watchlist empty、protected not-foundから`/`へ戻るlink |
| news | Dashboard / Watchlist / 関連記事の`PaperArticleCard`、Briefing内記事、Research citationから`/news/[id]`、detailから一覧へ戻るlink |
| Briefing | `BriefingBandCard`からcategory detail、detail / empty stateからBriefing一覧へ戻るlink |
| Admin | SettingsからPipeline Status / Source Healthへ移るlink |

次はglobal page pendingへ接続しない。

| 分類 | 対象 | feedback所有者 |
|---|---|---|
| query更新 | dashboard category / sort / per-page / pagination、Watchlist per-page / pagination、Source Health期間 | 各content boundary |
| Research内部 | new / thread / more | `ResearchNavigationBoundary` |
| mutation | login、watchlist、source追加・toggle・削除、Research送信・停止・thread削除、sign out、retry | 操作したcontrol。Research送信だけは同じpending sourceからworkspace statusも表示 |
| browser / 外部 | modifier / middle click、`target=_blank`、external URL、download、browser back / forward | browser |
| 同期UI | theme、drawer、popover、accordion等の開閉 | 即時state |

現行mutationのproduction変更対象は、feedbackがdisabledだけのResearch thread削除、実完了を観測できない
error retryの受付表現、button内だけでは受付が伝わりにくいResearch composerである。Login、Watchlist、
Source、sign outは既存feedbackを回帰確認し、必要なreduced-motion / accessible nameだけを整合させる。
Research composerはbutton pendingに加えてworkspace statusとredirect commitまでの表示連続性を整備する。

## Empty Fallback Policy

`fallback={null}`または高さだけの空要素は、次のいずれかを満たす場合だけ許可する。

1. optional decorative contentで、消えていても状態やlayoutの理解を損なわない。
2. 同じ領域の親にvisible pending statusがあり、空slotの寸法が固定されている。

現行箇所は次の方針で扱う。

| 現行箇所 | 決定 |
|---|---|
| root `Suspense` | neutral bootstrap fallbackへ置換 |
| dashboard masthead date | 固定寸法placeholderを維持してよい。親のcontent statusを共有 |
| dashboard result summary | text形状placeholderまたは`記事を更新中…`へ置換 |
| watchlist per-page select | select形状placeholderへ置換 |

## Accessibility

- page statusは`role="status" aria-live="polite" aria-atomic="true"`を常設し、textだけを更新する。
- pending対象の主要content rootへ`aria-busy=true`を付け、完了時に属性を外す。
- 同じ文言をlink、band、overlayの複数live regionから重複通知しない。
- visible statusをscreen-reader-only statusの代替にせず、両者を同じstateから導出する。
- spinner / skeletonはdecorativeとしてaccessibility treeから外す。
- pending開始時にfocusをstatusへ移さず、activated link / buttonのfocusを維持する。
- route commit後のfocusとscrollはNext.jsの標準navigation semanticsを維持する。Mobile Sheetのnavigation settle closeは
  triggerへのauto-focusを抑止し、手動closeだけtriggerへfocusを復帰する。
- `prefers-reduced-motion: reduce`ではspin、pulse、fadeを停止し、文言・contrast・borderだけで状態が分かる。
- pending stateは色だけで表さず、必ず`読み込み中…`等の文言を併記する。

## Fast Navigation and Error

### Fast navigation

- stateはinitiating Linkのpending=true観測時に開始する。prefetch済みでpendingがskipされた場合は開始しない。
- band / overlayはCSSで100ms程度のappearance delayを持てる。
- delay中にroute commitした場合は表示せずに終了してよい。
- pendingを見せるためのserver delayやminimum display timeは追加しない。
- prefetched routeで瞬時にcommitした場合、inline dotもbandも表示されなくてよい。

### Error

- page / RSC failureは既存route error boundaryへ任せる。
- 実際にcommitされたerror / not-found UIのclient notifierはmount時にcurrent global pendingをresetする。
- redirect後はinitiating Linkのpending終了またはcommitted URL observerで解除し、同じorigin URLへ戻るredirectでも
  元targetのbandを残さない。
- mutation errorは既存のinline error / toast方針を維持し、buttonを再操作可能へ戻す。
- error時に成功したようなdestination active表示へ先行更新しない。
- timeout時に旧画面へ黙って戻すfallbackは追加しない。

## Implementation Boundaries

責務は次の単位に分ける。名称は実装時に既存語彙との整合を確認するが、責務を混ぜない。

| 責務 | 所有するもの | 所有しないもの |
|---|---|---|
| app bootstrap fallback | root dynamic boundaryのneutral表示 | session、route target、feature data |
| page navigation provider | latest Link pending target、committed URL observer、band、error / not-found reset | router private API、overlay形状、feature mutation、Research内部lock、data fetch |
| pending-aware internal link | Link semantics、target / label、`useLinkStatus` lifecycle通知 | route rendering、可視global status |
| page content outlet | provider state由来のoverlay、`aria-busy` | target、router lifecycle、feature data |
| content loading state | 各画面のshape、visible status、`aria-busy` | global navigation target |
| mutation control | action pending、disabled、pending動詞 | page navigation status |
| Research operation coordinator | submission / 内部navigation / deleteのfirst-wins claimとrelease | global page target、run transport、DB mutation |
| Research submission boundary | composer pending、workspace status、accepted runの遷移予約、exact run modelのcommit | global page target、run transport、DB mutation |
| Research route host | retained / committed / rejectedの排他的outcome、workspace DOM identity | page data取得、run lifecycle、global navigation |
| Research stable answer slot | draft / finalizing / DB確定answerの原子的な表示置換、slot identity | terminal判定、polling / SSE、finalization retry schedule |
| Research Markdown configuration | 安定したcomponent / plugin / option identity、Markdown presentation | draft state、citation data、run lifecycle |

実装時の中心変更候補:

| 対象 | 変更責務 |
|---|---|
| `app/layout.tsx` | root `Suspense`へbootstrap fallbackを設定 |
| `components/layout/NonceThemeProvider.tsx` | nonce取得契約は維持。loading stateは所有しない |
| `(protected)/layout.tsx` | 認証順序を維持しつつpersistent page navigation boundaryを配置 |
| `(protected)/loading.tsx` | 削除。root bootstrapとfeature-local boundaryへ責務を移す |
| Admin配下の3つの`loading.tsx` | 削除。page内の既存skeletonへ一本化する |
| `components/layout/*Nav*` / Dashboard masthead | global targetを開始するpending-aware linkを利用 |
| `MobileNav.tsx` | pending中はSheetを開いたままtarget statusを表示し、観測可能なLink終了 / route commit後にcloseする。navigation settle closeだけtriggerへのauto-focusを抑止する |
| logo / article / Briefing / Research citation / Admin / back / empty-state links | 遷移先labelをglobal targetへ渡す |
| protected / route `error.tsx`・`not-found.tsx` | committed UI mount時のpending reset notifierを配置 |
| news / Briefing / Trends / Watchlist / Admin pages | data access近傍のshape fallbackを統一 |
| `research/layout.tsx` | persistent `PaperSurface` / masthead / workspace frame、初回workspace skeleton、global content outletを所有 |
| Research page / components | client-safe Public APIを介して内部navigation、operation coordinator、submission status、route outcome、stable answer slotを維持。外部記事linkだけglobal targetへ接続し、new-thread replace / refreshでの二重表示とblankを回帰確認 |
| Research Markdown components | renderer構成を安定化し、SSE delta間の不要なMarkdown subtree再mountを防ぐ |
| mutation controls | Research削除とretryを修正し、既存controlは回帰確認 |

新規dependencyは追加しない。React / Next.jsのstate、Context、`useLinkStatus`、`Suspense`、既存の
Tailwind utilityとUI primitiveだけで実装する。
`PendingAwareLink`はanchorとobserverだけの小さいClient Componentに留め、card / document / page全体を
client化しない。Server Componentのchildrenとserializableなtarget / labelだけを受け取る。
Link observerが使うstable lifecycle dispatchと、band / content outletが読むfeedback snapshotはcontextを分け、
記事card一覧の全Linkがglobal pending targetの変更だけで再renderされないようにする。

## Implementation Slices

### Slice 1: 共通feedback基盤

- root bootstrap fallback。
- persistent page navigation provider、Link lifecycle、status band。
- 全content outletと、desktop / Dashboard / Admin / mobile navのpending-aware link。
- latest-wins表示、Link lifecycle、URL commit、error / not-found notifierのcomponent test。
- route response遅延を使うdesktop / mobile E2E。

### Slice 2: 画面固有fallback

- protected共通とAdmin route `loading.tsx`の撤去。
- news detailの主content skeleton。
- dashboard / Briefing / Trends / Watchlistのshape、contrast、visible status修正。
- Research persistent layout、private-data-free workspace skeleton、`/research`と`/research/[threadId]`の
  shell重複所有解消。
- Admin in-page skeletonへの一本化。
- `fallback={null}` / 空spanの見直し。

### Slice 3: mutation / accessibility整合

- Research deleteの`削除中…`、Research submitのworkspace status、retryの受付feedback。
- 既存mutation controlのaccessible name / reduced motion回帰確認。
- `aria-busy` / live regionの重複整理。
- 全spinner / pulseのreduced motion対応。
- 認証済みPlaywright E2Eとproduction build検証。

### Slice 4: Research送信・回答確定の表示連続性

- 新規質問submitからclient navigation先thread commitまで、persistent shellと現在workspaceを保持するsubmission boundary。
- accepted completedからDB確定answer commitまで、draft / finalizing表示を保持するstable answer slot。
- Research Markdown rendererのcomponent / plugin / option identity安定化。
- 通常completed経路と新規質問navigationを対象にしたDOM identity component test、animation-frame E2E。
- 既存のSSE / polling / finalization state、retry間隔、DB正本、citation契約の回帰確認。

各sliceは独立してUXを改善し、前sliceの契約を壊さずmergeできること。Slice 2のためにSlice 1の
providerへfeature dataやskeleton構造を持ち込まない。Slice 4のstateをglobal navigation providerへ
持ち込まず、Research feature内で完結させる。

## Verification

### Component tests

1. same-tab internal linkの`useLinkStatus().pending=true`でtarget別statusを表示する。
2. `<Link>`のdefault navigation、href / replace / scrollとprefetch可能なanchorを維持する。
3. modifier click、middle click、external link、downloadではglobal pendingを開始しない。
4. pending A中にBをactivateすると最新target表示へ置換する。
5. prefetched Linkのpendingがskipされた場合は、新しいglobal stateを開始しない。
6. current `sourceKey`のLink pendingが`true -> false`になったときだけ解除し、initial false、同hrefの別Link、
   stale Aの終了でpending Bを解除しない。
7. targetまたはredirect先のpathname / search params commitでpendingを解除する。
8. committed not-found / route error notifierとprovider unmountでpendingを解除する。
9. mobile Sheetはpending中にtarget statusと利用可能な別linkを保ち、観測可能なLink終了 / commit後にcloseする。
   navigation settle closeではtriggerへfocusを戻さず、Escape / 手動closeでは戻す。
10. 各content outletがglobal pending時だけoverlayと`aria-busy`を持ち、masthead / navを覆わない。
11. Research表示中のShellNav Research itemはno-opで、workspaceの`new`だけがfeature-local navigationを開始する。
12. Research内部navigationは既存first-wins / overlayを使い、global statusへ二重登録しない。
13. content skeletonがvisible statusを1つ持ち、decorative nodeは`aria-hidden`になる。
14. Research削除buttonがspinner、`削除中…`、disabled、`aria-busy`を同時に持つ。
15. completion signalのないretryは`再試行中…`を固定時間表示せず、受付feedbackを通知する。
16. reduced motionでanimation class / styleを停止する。
17. Research submit中はbuttonの`送信中…`、composerの`aria-busy`、workspace内の
    `質問を送信しています…`が同じpending sourceから表示され、既存本文をDOMから外さない。
18. Research submitはclient navigation先または同threadでexact対象runがqueued / running /
    completed / failed / policy_blockedとしてcommitした時点でpendingを解除し、別thread / runでは解除しない。
    error時もstatus、disabled、`aria-busy`を残さず、global page navigation statusへ登録しない。
19. 同じ設定で`draftText`だけを更新したとき、Markdown mappingのcomponent typeと未変更blockのDOM node
    identityを維持する。parse結果の文字列一致だけで代替しない。
20. completed検知後のrefresh開始では同じanswer slot内にdraftと`回答を確定しています…`を維持し、
    DB確定answer反映時にslot wrapperを維持したままdraftだけを置換する。
21. finalization rerenderでdraftとfinal answerを同時表示せず、focus、scroll mode、
    `ResearchLiveAnnouncer`の1回通知契約を維持する。
22. submission pending中はResearch thread / new / more navigationとthread削除を開始せず、
    navigation pending中はsubmission / deleteを開始せず、delete pending中もsubmission / navigationを開始しない。
    delete失敗またはbutton unmountではcoordinatorをreleaseする。
23. Research not-found / error outcomeではretained workspaceまたは初回skeletonを同時表示しない。
    server renderのrejected marker、compiled CSS selector、hydration後のstate遷移を別々に確認する。

### Deterministic E2E

認証済みfixtureと、local / CIの`next dev`限定で「route response」と「feature data」を別々に2秒遅延させる
probeを使う。production codeにdelayを残さない。probeの所有者はE2E runnerとし、次の方式に固定する。

- route response: source page表示前にPlaywrightの`page.route`を登録し、対象routeのclick後RSCをgateで保持する。
  activationから250msのassertion後にgateをreleaseする。
- feature data: E2E runnerがNode標準libraryだけでlocalhost reverse proxyを起動し、frontend child processの
  `INTERNAL_API_URL`をprocess environmentでproxyへ向ける。`.env`は読まない・編集しない。
- proxyはscenarioごとに指定したbackend endpointだけを遅延し、session / auth endpointは遅延しない。これにより
  Server Component shellとmanual `Suspense` fallbackを先にstreamし、内側のdataだけを待たせる。
- `'use cache'`、Next Data Cache、browser router cacheのhitでproxyを迂回しないよう、各feature-data scenarioは
  E2E専用の空cacheを持つfresh frontend child processとfresh browser contextで開始する。Nextの出力先はtest-onlyの
  process environmentから、project内のvalidatedなworker / scenario固有directoryへ切り替え、通常の`.next`と
  production defaultは変えない。runnerが削除できるのは自ら作成したtest directoryだけとする。
- runnerはTCP acceptだけをreadyとせず、storage stateのcookieを付けた対象HTTP routeがcompileとresponseを
  完了してからgateをarmする。fresh distでGoogle Font fetchへ依存しないようNext公式の
  `NEXT_FONT_GOOGLE_MOCKED_RESPONSES`をtest-only fixtureへ向け、productionのfont設定は変えない。
- proxy gateは対象page requestより先に登録し、期待するbackend requestがgateへ到着したことを必須assertする。
  metadata / pageが同じendpointを複数回読む場合は、対象pathの全GETをreleaseまで保持して同じ固定outcomeを返し、
  hit signalだけを最初の1回に限定する。到着しない場合はfallback表示の成否にかかわらずtest failureとし、
  retryは同じwarm cacheを再利用せずfresh child / fresh test cacheで開始する。
- runnerはchild stdout / stderrを上限付きで収集し、失敗時に最初と最後のactionable outputを残す。
  `finally`でfrontend、proxy、scenario dist、fixtureを必ず停止・cleanupし、新規dependencyを追加しない。

route response probeではtarget shellをまだ返さず、activationから250ms時点を確認する。

| Scenario | 250ms時点の期待 |
|---|---|
| Dashboard desktop nav -> Research | 旧Dashboard、`Researchを読み込み中…`、content overlay |
| Dashboard mobile nav -> Briefing | Sheetを開いたまま`Briefingを読み込み中…`を大きく表示し、別linkも選べる |
| article card -> `/news/[id]` | 旧一覧、`記事を読み込み中…`、content overlay |
| Briefing card -> detail | 旧一覧、`Briefingを読み込み中…`、content overlay |

Researchのsubmission / finalizationはglobal route response probeへ混ぜず、認証済みResearch fixtureで
Server Action response、terminal通知、DB確定thread detailの解放時点を個別に制御する。test-supportは
local / CIでのみ有効にし、production API contractやrun処理を変更しない。

feature data probeではdata非依存shellを返した後、各manual boundaryのfallbackを確認する。

| Scenario | 期待 |
|---|---|
| `/` 初回 | Dashboard mastheadとarticle grid skeleton、visible status |
| `/news/[id]` | detail mastheadと`NewsDetailSkeleton`。Dashboard gridを表示しない |
| `/briefing/[category]` | Briefing detail skeleton。Dashboard gridを表示しない |
| `/research/[threadId]` 初回 | private情報を含まない`ResearchWorkspaceSkeleton` |
| `/settings` / `/admin/*` | 対象pageと同形状のin-page skeleton。route loadingを二重表示しない |
| dashboard category変更 | page bandなし、article領域に`記事を更新中…`とshape skeleton |

競合・終了経路はcontrolled responseで確認する。

| Scenario | 期待 |
|---|---|
| pending A中にBを選び、Aを先にresolve | AをcommitせずBのband / overlayを維持し、最終的にBだけをcommitする |
| `/`からtargetが`/`へredirect | initiating Link終了を観測し、元targetのstatus / overlayが残らない |
| `PaperArticleCard`からdetail 404 / 500 | actual `PendingAwareLink`でglobal status / overlay / `aria-busy`を開始し、news not-found / error後に全解除する |
| Research thread A -> B | 既存のA本文、B対象overlay、global statusなし |
| Research thread A -> Bが404 / 500 | A本文とB対象local overlayを保持し、route固有outcome後にAとlocal / global pendingを残さない |
| Research 404 / 500をdirect hard-load | initial skeletonからroute固有outcomeへ移り、rejected marker出現時にinitial / retained領域を同じframeで可視化しない |
| Research thread上でShellNav Researchを選ぶ | no-opのままthreadを保ち、global / local pendingを開始しない |
| `/research`で新規質問submit、accepted Action responseを保留 | 同じResearch shellとworkspace、`質問を送信しています…`、pending composerを維持し、route / workspace skeletonと空白を表示しない |
| 新規質問の最初のthread modelがfailed | exact対象runのcommitでsubmission statusとlockを解除し、別runでは解除しない |
| 既存threadで質問submit、active run反映を保留 | 既存message、workspace status、pending composerを維持し、global statusを表示しない |
| 通常run completed、確定thread detail反映を保留 | 同じanswer slotのdraftと`回答を確定しています…`を維持し、route skeletonと空白を表示しない |
| 確定thread detailを解放 | 同じshell / composer / scroller / answer slotのままDB確定answerだけへ置換し、draftとfinalを同時表示しない |
| Research delete | dialog actionに`削除中…`、二重操作不可 |

全scenarioで完了後にstatus / overlay / `aria-busy`が消え、正しいURL、active state、contentになることを
確認する。

Researchの新規submitと通常completedは、操作開始前にpersistent対象のelement referenceを取得し、
response保留からcommit完了まで`requestAnimationFrame`ごとに次を記録・assertする。新規submitは
`PaperSurface`、masthead、workspace frame、composerを対象とし、既存thread submitではmessage scroller、
通常completedでは対象answer slotも加える。

1. persistent対象nodeが`isConnected=true`で、主要領域のbounding boxが0にならない。
2. persistent対象nodeのreferenceがroute commit / RSC merge後も同一である。
3. answer slot内はdraft / finalizingまたはfinal answerのどちらかが常に可視で、空のframeと同時表示がない。
4. `(protected)/loading.tsx`由来のnews skeleton、`ResearchWorkspaceSkeleton`、global page bandが途中で現れない。

既存のfailure terminal continuityだけで通常completedを代用せず、DB確定answerまで到達する成功fixtureを
必須にする。testがDOM snapshotの前後比較だけで中間blankを見逃さないよう、frame sampleまたは同等の
連続観測を受け入れ条件とする。

### Initial load / build

1. production buildのgenerated static shell、または動的routeのfirst streamed fallback chunkに
   `画面を準備しています…`が含まれる。
2. `AppBootstrapLoading`のrender結果とfirst fallback chunkだけをprivacy assertionの対象とし、認証済みuser情報、
   記事、Research thread情報を含めない。後続の認可済みRSC / HTML stream全体はこのassertionの対象外とする。
3. 未認証response全体にprotected dataが含まれない既存security testを維持する。
4. root fallback中もCSP nonce生成とresponse headerが従来どおり有効である。
5. protected routeのdata fetchが`requireSession()` / `requireAdmin()`より前に始まらない。
6. `/news/[id]`、`/briefing/[category]`、`/research*`、Adminで`記事を読み込み中`を誤表示しない。
7. desktop / mobile、light / dark、`prefers-reduced-motion`でvisual regressionを確認する。
8. Cache Componentsのbuild artifactでmanual fallbackを含むstatic shell / RSC payloadを確認し、default prefetchを
   一括で無効化していないことを確認する。
9. `/check`を実行し、Biome、TypeScript、component test、production build、対象E2Eを通す。

## Acceptance Checklist

- [x] serverから最初のHTML shellを受信した後のfirst paintに、空白ではなくbootstrap fallbackがある。
- [x] desktop / mobileの主要page navigationは、クリック後100msを超える待機で可視statusを持つ。
- [x] mobile menuはpending中にtarget statusを表示し、公開signalで観測できるnavigation settle後に自動で閉じる。
- [x] 旧画面を保持できるclient transitionでは、空白や無関係skeletonへ即時置換しない。
- [x] protected共通とAdmin route `loading.tsx`がなく、root + feature-local fallbackへ責務が分かれている。
- [x] news detailに主記事用fallbackがある。
- [x] Briefing / Trends / Watchlist / Research / Adminのfallbackが実画面の最初のviewportと対応する。
- [x] latest-wins表示、same-URL redirect / not-found / errorの終了契約がE2Eで固定される。
- [x] Research内部の既存pending / lock / overlay契約が維持される。
- [x] Research新規質問の送信受付から遷移先commitまで、persistent shellと現在workspaceが消えず、
      主content内に`質問を送信しています…`が見える。
- [x] Research submissionは対象runがactive / terminalのどのstatusで最初にcommitされてもexact identityで終了し、
      submissionと内部navigation / deleteのfirst-winsが維持される。
- [x] Research通常completedで、draft / `回答を確定しています…` / DB確定answerが同じstable answer slot内で
      空frameを挟まず置換される。
- [x] Researchのshell / composerはnew-thread replaceとRSC refresh、既存scrollerはsubmit / refresh、answer slotは
      finalization refreshをまたいでDOM identityがcomponent testとanimation-frame E2Eにより固定される。
- [x] SSE draft更新でMarkdown component typeを再生成せず、未変更blockを設定identityの変化で再mountしない。
- [x] link横spinnerを唯一のpage feedbackとして使わない。
- [x] 非optimistic mutationはcontrol-localなspinnerまたはpending動詞を持ち、Research thread削除は両方を持つ。
- [x] statusはvisible textとlive regionを同じstateから導出する。
- [x] reduced motionで状態の意味が失われない。
- [x] nonce CSP、認証、認可、API、DB、cache契約を変更していない。
