# Research thread 切替 pending UI 仕様

## 位置付け

親仕様: `backend/specs/agent-history-thread-ui-slice.md`。

本仕様は、`/research` のスレッド切替・新規スレッド画面への遷移・一覧の「さらに表示」で、
遷移中であることを明示し、旧スレッドに対する操作を止める UI slice である。product codeの
変更はfrontendに閉じるが、決定的なE2Eデータのためbackend test-support scriptを追加する。
会話データの取得元、API response、DB schema、run の状態機械は変更しない。

Status: Implemented（component / page-model / 認証済みPlaywright E2E検証済み）

## Problem

Research のスレッドリンクは Next.js の通常の `<Link>` だが、遷移状態を UI に接続して
いない。動的 route の RSC 応答を待つ間、URL・active 表示・詳細本文は旧スレッドのまま
で、ユーザーにはクリックが受理されたか分からない。

さらに、旧スレッドの削除、質問入力、別スレッド選択、新規スレッド作成が操作可能なため、
「表示中のスレッド」と「移動しようとしているスレッド」の境界が曖昧なまま mutation や
重複 navigation を起こせる。

## Work Definition

### Problem

スレッド切替中の対象・進行状態・操作不能範囲を、視覚表示とアクセシビリティ契約の両方で
明確にする。

### Evidence

- 実ブラウザで route 応答を 2 秒遅延させ、クリック 250ms 後の DOM と画面を確認済み。
- 一時的なResearch専用 `loading.tsx` と3秒のserver-side page delayを使い、A→B中に
  route fallbackが旧本文を置換することを250ms / 1.5秒時点で確認済み。
- 現行の `ResearchSidebar`、`ResearchWorkspace`、`ResearchComposer`、
  `DeleteThreadButton`、親 `(protected)/loading.tsx` を確認済み。
- Next.js 16 App Router の `Link.onNavigate`、`useLinkStatus`、`loading.tsx`、
  dynamic route prefetch の公式仕様を確認済み。
- Vercel Web Interface Guidelines の async update、navigation semantics、reduced motion、
  loading copy の規則を確認済み。

### Invariants

- 確定前の遷移先を active thread として表示しない。
- pending 中に旧スレッドへの mutation を開始できない。
- 1 回の Research navigation 中に別の Research navigation を開始できない。
- `<Link>` の semantic、通常の prefetch、modifier click・middle-click を維持する。
- 会話・一覧の描画データは Server Components から取得し、client fetch を追加しない。
- shell navigation は pending 中も利用でき、Research 画面から離脱できる。
- loading indicator の有無でレイアウトが移動しない。

### Non-goals

- backend API、Pydantic schema、生成 TypeScript 型、DB schema の変更。
- run 実行中表示（`hasActiveRun` / `ActiveRunStatus`）の語彙・polling の変更。
- 質問送信中に sidebar navigation まで止める双方向 lock の追加。
- browser back/forward、shell navigation を含む全アプリ共通 navigation manager。
- Research 以外にある loading UI の全面再設計。
- `(protected)/loading.tsx` のnews固有fallbackをroute-neutralにする変更。
- optimistic thread content、client-side thread cache、状態管理ライブラリの追加。

### Done

- 2 秒遅延時、クリック対象と「読み込み中…」が視覚的に分かる。
- pending 中、Research 内の navigation と mutation controls が操作不能になる。
- 支援技術へ target を含む loading status が通知される。
- navigation 完了後、正しい thread が active になり controls が復帰する。
- thread切替中は旧本文＋inline overlayを維持し、route fallbackへ置換しない。
- local/CI限定の決定的なResearch E2E fixtureを投入・削除できる。
- component test と認証済み Playwright E2E で上記契約が固定される。

## Current Evidence

### 実ブラウザ測定

`A → B` の RSC request を 2 秒止め、クリック 250ms 後を測定した。

| 観測項目 | 実測 |
|---|---|
| URL | A の URL のまま |
| detail heading | A の title のまま |
| B link `aria-busy` | なし |
| B link `aria-disabled` | なし |
| B link pointer events | `auto` |
| pending marker | 0 件 |
| visible `role=status` | 0 件 |
| 「新しいスレッド」 | 操作可能 |
| 「スレッドを削除」 | enabled |
| 質問 textarea | enabled |
| navigation 完了後 | B の URL / title / content に正常切替 |

表示上は旧スレッドが完全に idle に見え、クリック対象にも変化がない。ユーザーが感じた
「ロードしているか分からない」「ボタンが無効化されていない」は再現した。

### `loading.tsx` 共存probe

Research専用 `loading.tsx` を一時追加し、Bのpage処理自体を3秒遅延させてA→Bを測定した。
probe用file、delay、fixture dataは測定後に全て削除済み。

| 時点 | URL | 旧A本文 | Research route fallback |
|---|---|---|---|
| 250ms | A | 表示 | なし |
| 1.5秒 | B | なし | 表示 |
| 完了後 | B | なし | なし、B本文を表示 |

Next.jsのroute transitionでも動的parameter変更時にloading boundaryがresetされ、途中から
fallbackが旧本文を置換する。このためResearch専用 `loading.tsx` と「旧本文＋inline overlay」
を同じthread切替契約には含めない。

### 現行コード

- `ResearchSidebar.tsx`: thread / new / more は plain `<Link>`。pending state なし。
- `ResearchWorkspace.tsx`: route navigation の pending boundary なし。
- `ResearchComposer.tsx`: disabled は submit/cancel pending と active run だけで決まる。
- `DeleteThreadButton.tsx`: disabled は delete action 自身の pending だけで決まる。
- `(protected)/loading.tsx`: news 用 skeleton を全 protected route に使い、status 文言も
  「記事を読み込み中」。Research 固有の意味を表さない。
- `[threadId]/page.tsx`: thread detail 完了後に thread list を取得する直列 await。

### Web Interface Guidelines review

```text
frontend/src/features/research/components/ResearchSidebar.tsx:69 - async navigation の status / busy state がない
frontend/src/features/research/components/ResearchSidebar.tsx:69 - active link に aria-current がない
frontend/src/features/research/components/ResearchWorkspace.tsx:20 - async update を aria-live で通知しない
frontend/src/features/research/components/ResearchComposer.tsx:75 - route pending を disabled 条件に含めない
frontend/src/features/research/components/DeleteThreadButton.tsx:53 - route pending を disabled 条件に含めない
frontend/src/app/(protected)/loading.tsx:18 - Research route でも news の loading copy を通知する
```

## Research Results

### Next.js 16

1. Dynamic route は server response を待つ。`loading.tsx` がある場合は最寄りの loading
   boundaryまでpartial prefetchされ、page以下をSuspense fallbackで置換できる。
2. Transitionは既に表示済みのUIを通常は保持するが、routerが別route parameterとして
   Suspense boundaryをresetした場合はfallbackが表示される。現行Next.js 16.2.9でも
   `/research/A → /research/B` のserver-side delayでこの置換を実測した。
3. `useLinkStatus()` は `<Link>` の descendant だけで使える link-local signal である。
   prefetched route では pending が省略され、複数 link では最後の link だけが pending に
   なるため、Research 全体の操作 lock の SSoT には使えない。
4. `Link.onNavigate` は same-origin SPA navigation のときだけ発火する。Cmd/Ctrl+click、
   download、external navigation では発火しないため、anchor semantics を維持したまま
   same-tab transition だけを制御できる。
5. `onNavigate.preventDefault()` はLink由来のnavigation dispatch前に処理を終了する。
   その後の手動 `router.push` は元のLinkのpendingではないため、`useLinkStatus` は
   visual hintを含め本設計では利用しない。
6. 本リポジトリには `useTransition(() => router.push(...))` で disabled と pending feedback
   を構築する先例が `lib/search-params/client.ts` にある。

参照:

- [Next.js Linking and Navigating](https://nextjs.org/docs/app/getting-started/linking-and-navigating)
- [Next.js Link](https://nextjs.org/docs/app/api-reference/components/link)
- [Next.js useLinkStatus](https://nextjs.org/docs/app/api-reference/functions/use-link-status)
- [Next.js loading.js](https://nextjs.org/docs/app/api-reference/file-conventions/loading)
- [React Suspense](https://react.dev/reference/react/Suspense)
- [Vercel Web Interface Guidelines](https://github.com/vercel-labs/web-interface-guidelines)

## UX Contract

### State model

```text
idle
  └─ onNavigate(target) ─> navigation_pending(target)
                              ├─ route commit ─> idle(new route)
                              ├─ route error  ─> existing route error boundary
                              └─ shell nav    ─> Research boundary unmount
```

`target` は以下の discriminated union とする。

```text
thread: { href, threadId, label }
new:    { href: "/research", label: "新しいスレッド" }
more:   { href, label: "さらに表示" }
```

同時に存在できる target は 0 または 1。pending 中の Research navigation は最初の 1 件を
採用し、後続は開始しない。shell navigation はこの lock の外に置く。

first-winsは、遷移中のResearch controlsを無効化するproduct要件を満たし、複数RSC request、
target表示の差し替わり、完了順競合を避けるために選ぶ。誤クリックの訂正可能性より、1件の
遷移対象と操作不能範囲が一意であることを優先する。Research外へ離脱するshell navigationは
lockしない。latest-winsへの変更はこのinteraction contractの変更として別途判断する。

### Thread switch

1. active thread 以外の row を same-tab でactivateすると pendingを開始する。
2. URLとactive stylingはroute commitまで現在threadのままにする。
3. クリック対象rowではtimestampを `読み込み中…` に置換し、固定幅のspinnerを表示する。
   クリック対象はaccent border / tint / ringで最も強く表示し、旧activeを含む他rowは
   opacityを下げる。commit前のactiveと遷移targetを視覚的に混同させない。
4. clicked row は `aria-busy=true`。他のResearch linksを含めnavigation controlsをlockする。
5. detail paneには半透明overlayを置き、`「{thread.title}」を読み込み中…` を表示する。
   旧本文を消して空白にせず、更新中であることと旧内容であることを明示する。
6. commit後はoverlayを外し、target threadへ `aria-current=page` を移す。

`hasActiveRun` の spinner と navigation spinner が同じrowで競合する場合、navigation
pendingを優先する。文言 `読み込み中…` を必ず併記し、回答生成中との意味を混同させない。

### New thread navigation

- `+` linkをactivateするとiconをspinnerへ置換する。
- detail pane overlayは `新しいスレッドを準備中…` とする。
- `usePathname()` が `/research` ならsearch paramsの有無にかかわらずnavigationを開始しない。
  既にnew-thread composerを表示しているため、`?limit=` をresetする目的では使わない。

### Load more navigation

- load more linkのlabelを `読み込み中…` に変更しspinnerを表示する。
- 現在のsidebar listはcommitまで保持する。
- pending中はthread/new/moreの全navigationをlockする。

### Fast navigation

pending stateとoperation lockはnavigation受付時に即時開始する。indicator用の領域はidle時も
確保し、opacity/transformだけを変える。高速遷移で短く表示されても、layout shiftを
起こさないことを優先し、時間ベースのminimum displayや人工的delayは追加しない。

### Error

- backend / RSC failureは既存 `(protected)/error.tsx` に任せる。
- error画面へ遷移した時点でResearch boundaryは置換され、pending lockを残さない。
- errorをtoastへ変換したり旧threadへ黙って戻すfallbackは追加しない。

## Interaction Lock Contract

pending 中の状態は次のとおり。

| Control | pending中 | 実装契約 |
|---|---|---|
| clicked thread link | locked + busy | `aria-busy=true`, `aria-disabled=true`, activation guard |
| other thread links | locked | `aria-disabled=true`, activation guard |
| active thread link | current/no-op | `aria-current=page`, redundant navigationを開始しない |
| new thread link | locked | `aria-disabled=true`, activation guard |
| load more link | locked | `aria-disabled=true`, activation guard |
| delete trigger | disabled | native `disabled` |
| delete dialog action | disabled | native `disabled`、dialog表示後にpendingへ入る経路もguard |
| composer textarea | disabled | native `disabled` |
| send / stop button | disabled | native `disabled` |
| citation / external links | enabled | stale data mutationではなく、Research外へ離脱可能 |
| shell nav / theme / sign out | enabled | Research boundary外。離脱経路を残す |

`aria-disabled` は機能を停止しないため、link wrapperはevent guardも必須とする。pending中の
Research linkは通常のpointer clickとkeyboard activationを拒否する。一方、Cmd/Ctrl/Shift/Alt
clickとmiddle-clickは `onNavigate` が発火しないため、idle/pendingにかかわらずbrowser defaultを
維持する。これらは別tab/windowのnavigationでcurrent tabのpending targetを変更しない。

## Accessibility

- workspace `<main>` に `aria-busy={isNavigationPending}` を付ける。
- feature boundary内に常設の `role=status aria-live=polite aria-atomic=true` を置く。
- pending text:
  - thread: `「{title}」を読み込み中…`
  - new: `新しいスレッドを準備中…`
  - more: `スレッド一覧を読み込み中…`
- spinnerはdecorativeとして `aria-hidden=true`。意味はvisible text / live regionが持つ。
- active thread linkには `aria-current=page` を付ける。
- pending開始時にfocusを移動しない。clicked linkのfocusを保持する。
- animationは `motion-reduce:animate-none` / `motion-reduce:transition-none` 相当を持つ。
- copyは3点リーダーではなく単一文字 `…` を使う。
- color変化だけに依存せず、text + spinner + busy semanticsを併用する。

## Route-level Loading Decision

本sliceでは `frontend/src/app/(protected)/research/loading.tsx` を追加しない。実測上、
dynamic thread切替の途中でroute fallbackが旧本文を置換し、inline overlay、clicked target表示、
feature-local lockを所有するpage subtreeもunmountし得るためである。

- thread / new / moreのnavigation feedbackは `ResearchNavigationBoundary` だけが担当する。
- A→Bのpending中は旧A本文＋inline overlayを維持し、skeletonへ切り替えない。
- E2Eで `記事を読み込み中` とResearch route skeletonが表示されないことを固定する。
- direct load / reloadは現行の親 `(protected)/loading.tsx` のままとする。
- 親fallbackのnews固有copyは既知の問題だが、route-neutralなprotected shellへの変更は
  Research外にも影響するため別仕様で扱う。

## Frontend Structure

```text
backend/scripts/
  seed_e2e_research.py             # local/CI限定の決定的なthread fixture

frontend/scripts/
  run-research-e2e.mjs             # seed→Playwright→finally cleanup runner

frontend/src/features/research/components/
  ResearchNavigationBoundary.tsx   # client: <main> + useTransition + context + status/overlay
  ResearchNavigationLink.tsx       # client: Link全体 + meta切替 + onNavigate/disabled guard
  ResearchWorkspace.tsx            # server: boundaryへsidebar/detailをchildrenとして渡す
  ResearchSidebar.tsx              # server: serializableなrow表示値をclient linkへ渡す
  ResearchComposer.tsx             # client: navigation pendingをdisabled条件へ追加
  DeleteThreadButton.tsx           # client: navigation pendingをdisabled条件へ追加
```

### Boundary

`ResearchNavigationBoundary` はfeature-local contextを持つ。global storeやapp-wide providerは
追加しない。現在 `ResearchWorkspace` が所有するworkspace `<main>` はboundaryへ移し、
`aria-busy`、live status、detail overlayを同じclient boundaryで制御する。serverから渡す
sidebar/detailのchildrenは描画するだけで、その内部をclient側から解析・変更しない。

```text
isNavigationPending: boolean
pendingTarget: ResearchNavigationTarget | null
navigate(target): void
```

`navigate` は `startTransition(() => router.push(target.href))` を実行する。targetはtransition
開始前に記録し、表示は `isNavigationPending ? pendingTarget : null` から導出する。transition
完了後にstale targetが残ってもUIへ露出させない。

Nextのroute cacheは以前のthread subtreeを非表示状態で保持し、再訪時にclient stateも復元する。
boundaryのeffectがcacheへの退避でcleanupされ、復帰時にsetupされるライフサイクルを使い、
復帰したinstanceのnavigation lockとpending targetを初期化する。加えてpending中だけ実際の
`window.location` commitを監視し、旧subtree側の`usePathname`が古い値を保持する場合もcleanupする。
`useTransition().isPending` はRSC完了前にfalseへ戻り得るため、commit判定には使わない。

### Link

`ResearchNavigationLink` は `<Link>` を保持し、`onNavigate` でsame-tab SPA navigationだけを
boundaryへ渡す。thread rowはserverから `title`、format済み `idleMetaLabel`、
`hasActiveRun`、targetをserializable propsとして受け、client側でrow全体を描画する。
pending時は `idleMetaLabel` の領域を `読み込み中…` に切り替える。server children内の
timestampを探索・書き換える実装にはしない。

- idle + non-active: `event.preventDefault()`後に`navigate(target)`。
- active: `event.preventDefault()`、navigationを開始しない。
- pending: `event.preventDefault()`、後続navigationを開始しない。
- modifier click / middle-click: pending中を含め `onNavigate` 非発火のためbrowser defaultを維持。
- prefetchはdefaultのまま。`prefetch=false`にしてpendingを長く見せない。

`useLinkStatus` は使わない。prefetch済み時にpendingがskipされglobal lockを表せないことに
加え、本設計は `onNavigate.preventDefault()` でLink由来のdispatchを止めるため、元のLinkは
pendingにならない。pendingのSSoTはboundaryの `isNavigationPending` だけとする。

### Server data loading

`/research/[threadId]` のthread detailとthread listは互いに独立しているため、同一component内の
直列awaitを同時開始へ変更する。単純な `Promise.all` は先にrejectしたrequestで結果が変わる
ため使わず、`Promise.allSettled` 相当で両結果を受けて次の順に評価する。

1. detailが `ApiError(404)` なら `notFound()`。
2. detailがその他のerrorならそのerrorをthrow。
3. listがerrorならそのerrorをthrow。
4. 両方成功した場合だけrenderする。

これにより現行のdetail-first error precedenceを保ち、listの404/5xxをthread 404へ誤変換
しない。detail 404とlist failureが同時に起きても、結果は必ず `notFound()` とする。

これはpending時間を短縮するが、loading UIを不要とする根拠にはしない。両requestの
`cache: no-store` とAPI contractは不変。

## Tests

### Component tests

1. `ResearchNavigationBoundary`
   - workspace `<main>` を所有し、idleでは `aria-busy=false`、status textなし。
   - navigateでtargetを保持し、transition中 `aria-busy=true`。
   - `router.push`へ正しいhrefを1回だけ渡す。
   - pending中の2回目navigateを拒否する。
   - pending中もserverから渡された旧detail childrenとinline overlayを同時に描画する。
2. `ResearchNavigationLink`
   - semantic `<a href>`を維持する。
   - activeは `aria-current=page` でsame-route navigationを開始しない。
   - pending targetは `aria-busy/aria-disabled` と `読み込み中…` を持つ。
   - pending中の通常click / keyboard activationでは他linkをactivateできない。
   - pending中もmodifier click / middle-clickはpreventせずbrowser defaultへ渡す。
   - `idleMetaLabel` だけがpending textへ置換され、title等のrow構造は維持される。
   - pathnameが `/research` なら `?limit=` があってもnew-thread navigationはno-op。
3. `ResearchComposer`
   - navigation pendingでtextareaとsend/stopがdisabled。
   - 既存のsubmit/cancel pendingとactive run条件を壊さない。
4. `DeleteThreadButton`
   - navigation pendingでdialog triggerとdialog内delete actionがdisabled。
   - delete action pendingの既存lockを壊さない。
5. page data loading
   - detailとlistが同時に開始される。
   - detail 404だけが `notFound()` へ収束する。
   - detail 404とlist failureが同時でも `notFound()` へ収束する。
   - detailの非404 errorはlist errorより優先してthrowされる。
   - detail成功時だけlist errorがthrowされる。

### Playwright E2E

認証済み `user` projectで3件のcompleted Research threadを固定fixtureとして用意する。
`backend/scripts/seed_e2e_research.py` に `seed` / `cleanup` subcommandを実装し、既存
`seed_e2e_users.py` の固定userを所有者として、固定UUIDのthread、user/assistant messages、
completed runを投入する。

- `ENV=production` では既存E2E user seedと同様にfail-fastする。
- `seed` はupsertまたは存在確認により再実行可能とし、実AI providerやworker enqueueを使わない。
- `cleanup` は固定thread UUIDだけを削除し、FK cascadeでfixture子行だけを除去する。
- frontend testからDBへ直接接続しない。
- `frontend/scripts/run-research-e2e.mjs` がseed script、Playwright、cleanup scriptを順に
  subprocess実行し、`finally` でtest成否にかかわらずcleanupする。
- runnerはPlaywrightのexit codeを呼び出し元へ返し、cleanup failureも成功として隠さない。
- 通常のlocalデータをseed/cleanupの更新・削除条件に含めない。

このscriptだけがfrontend-only product sliceの例外となるtest supportであり、API・schema・
runtime repositoryには変更を加えない。

fixtureの `updated_at` は `A > B > C` に固定し、初期URLを
`/research/A?limit=2` とする。これにより一覧へA/Bを表示しつつ、`total=3` による
「さらに表示」も同じ画面で決定的に検証できる。

`frontend/e2e/research.spec.ts` で以下を検証する。

1. Aを表示してBのRSC requestを2秒遅延する。
2. Bをclick後、pending中に以下をassertする。
   - B rowに `読み込み中…` とbusy state。
   - workspace `aria-busy=true`。
   - live statusがtarget titleを含む。
   - delete、textarea、send/stopがdisabled。
   - A/B/new/moreの通常activationによる追加navigationが拒否される。
   - old detail paneにloading overlayがある。
   - old detail contentがDOMに残り、`記事を読み込み中` とroute skeletonは表示されない。
   - modifier click / middle-clickは別tab/windowのbrowser defaultへ渡り、current tabの
     pending targetはBのまま変わらない。
3. response解放後、URL・heading・content・`aria-current`がBへ切り替わる。
4. controlsと`aria-busy`がidleへ戻る。
5. Console errorが0件。
6. cache無効のhard reload後に `A → B → A` と往復し、2回目のcommit後もvisible workspaceが
   `aria-busy=false`、overlay非表示、Aがactiveであることを確認する。

失敗時のtrace/screenshotは既存Playwright設定に従い保存し、storageStateはcommitしない。

## Verification

実装後は以下を実行する。

```bash
cd frontend
npx biome check src/
npx tsc --noEmit
npm test
E2E_BASE_URL=http://localhost:3000 node scripts/run-research-e2e.mjs
```

E2E runnerはtest成功・失敗の両方でcleanupを実行する。手動/対話検証でも2秒delayを入れ、
250ms時点、1.5秒時点、完了後を確認する。高速localhostだけで「表示されたように見える」を
合格根拠にしない。

## Done Checklist

- [x] thread switchのclicked targetに `読み込み中…` が表示される。
- [x] workspaceが視覚・ARIAの両方でbusyになる。
- [x] pending中のResearch navigationが1件に制限される。
- [x] pending中のdelete/composer controlsがdisabledになる。
- [x] active threadが `aria-current=page` を持つ。
- [x] modifier click / middle-clickのlink semanticsが維持される。
- [x] thread切替中に旧本文＋overlayが維持され、route fallbackへ置換されない。
- [x] detail/list fetchが並列化される。
- [x] detail/listの同時failureでもerror precedenceが決定的である。
- [x] local/CI限定E2E fixtureを冪等にseed/cleanupできる。
- [x] reduced motionでpending animationを停止できる。
- [x] component testsと認証済みPlaywright E2Eがgreen。
- [x] API/DB/generated typesに変更がない。
