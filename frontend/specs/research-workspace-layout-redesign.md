# Research workspace 固定シェル・レスポンシブ再設計仕様

Status: Implemented（2026-07-13、実機iOS Safari/PWAのkeyboard確認のみ未実施）

## 位置付け

本仕様は、`/research` と `/research/[threadId]` の画面構成を、履歴・表示内容・入力欄が
それぞれ明確な責務を持つ固定ワークスペースへ変更する frontend UI slice である。

参照する既存仕様:

- `backend/specs/agent-history-thread-ui-slice.md`
- `frontend/specs/research-thread-navigation-pending.md`
- `frontend/specs/agent-research-live-ui-slice.md`

Codex / Perplexity の画面は、履歴を独立領域に置き、本文だけをスクロールし、入力欄を
ワークスペース下部に残す情報構成の参考とする。配色、文字、紙面テクスチャ、citation
表現は既存の Vector デザインを維持し、参考製品の固有機能や見た目を複製しない。

本仕様は次の既存配置だけを限定的に上書きする。

- `agent-research-live-ui-slice.md` の「工程・activity を user message card 内に置く」契約を、
  「質問 box 直下の独立した run status rail に置く」契約へ変更する。
- 回答本文の表示状態に左右されないよう、分散しているvisible statusのARIA通知責務を
  thread pane内の単一announcerへ移す。通知語彙と頻度は既存契約を維持する。
- live draftからDB確定回答への収束では、transportの安全契約を維持したままstable answer slotと
  visual anchorを保つ。通常deltaのauto-followとfinal answer置換のscroll契約を分離する。
- `agent-history-thread-ui-slice.md` の常設 2 ペインを、desktop の開閉可能 sidebar と
  compact viewport の modal drawer へ拡張する。

SSE、polling、navigation pending、API、DB の既存契約は上書きしない。競合時は、画面配置・
responsive layout・ソース一覧の配置についてだけ本仕様を正とし、それ以外は既存仕様を正とする。

## Work Definition

### Problem

現在の Research 画面には次の問題がある。

1. masthead 後の route wrapper が再度 `min-h-dvh` を持つため document scroll が発生し、
   履歴と composer が画面全体のスクロールに追随する。
2. 履歴を開閉できず、狭い viewport でも会話領域の上に常設される。
3. 回答と確定済みソースを同じ会話列へ並べているため本文が長くなり、ソース一覧を参照するために
   回答の読書位置を動かす必要がある。
4. composer は会話 scroller の外にあるが、viewport shell が成立していないため画面下部に
   安定して残らない。
5. 工程・activity・失敗表示が質問 box の内側にあり、live update で質問 box 自体の高さが
   変わる。
6. compact viewport 用の履歴 drawer、safe area、responsive regression test がない。
7. 回答完了時にlive subtreeがDB確定済みmessage subtreeへ置き換わり、確定回答以外の要素も同時に
   追加されるため、回答欄が一度消えた後に画面全体が切り替わったような視覚的跳躍が発生する。

### Evidence

- `frontend/src/components/layout/SlimMasthead.tsx`
  - masthead は通常 flow 内の高さ 58px。
- `frontend/src/app/(protected)/research/page.tsx`
- `frontend/src/app/(protected)/research/[threadId]/page.tsx`
  - masthead の後ろに `min-h-dvh` wrapper を置いている。
- `frontend/src/features/research/components/ResearchNavigationBoundary.tsx`
  - workspace は別の `calc(100dvh - 5.5rem)` と `overflow-hidden` を持つ。
- `frontend/src/features/research/components/ResearchSidebar.tsx`
  - 履歴 nav 自体は `overflow-y-auto` だが、sidebar は常設で開閉 state を持たない。
- `frontend/src/features/research/components/ResearchThreadView.tsx`
  - thread header、message scroller、composer を縦配置する。
  - active run の status を `UserMessage` の質問 box 内へ注入する。
  - composer の入力行がthread pane全幅へ広がり、回答本文のrailと揃っていない。
- `frontend/src/features/research/components/ResearchThreadLiveBoundary.tsx`
  - message scroller と live auto-follow を所有する。
  - `finalContentKey`変更を通常のanswer updateとして扱い、末尾付近では更新後の
    `scrollHeight`絶対末尾へscrollする。
- `frontend/src/features/research/components/LiveAnswerDraft.tsx`
  - 通常completedではvisible draftをfinal描画まで保持する。
  - terminal event前のEventSource `CLOSED`ではcontrollerがdraftをsuppressedにするため、polling中の
    `running + suppressed`表示が`null`となりassistant領域が空になる。
- `frontend/src/features/research/components/ResearchThreadView.tsx`
  - RSC refresh後はactive run boundary、user message、live draftのsubtreeから、user messageと
    DB確定済みassistant messageのsubtreeへ切り替わる。
- `frontend/src/features/research/components/ResearchThreadView.test.tsx`
  - 通常terminal後のdraft保持と同期的なDB final置換は保証するが、paint frame、RSC refresh、
    final追加時のbounding box / scrollTop continuityは保証していない。
- `frontend/src/features/research/components/ResearchComposer.tsx`
  - submit / cancel / navigation pending の操作 lock を所有する。
- 実ブラウザ測定:
  - 390×844 で document `scrollHeight = 903`。
  - 1440×900 で document `scrollHeight = 959`。
  - いずれも viewport より masthead 相当の約 59px 長い。

API の SSoT `backend/app/schemas/research.py` から表示できる情報は次に限定される。

- thread: title、updatedAt、hasActiveRun、pagination
- user message: content、createdAt、run status / errorCode / progressStage
- assistant message: content、createdAt、sources、missingAspects
- source: internal article または external URL の既存 field
- live progress: 検証済み stage / known activity / draft / terminal

画像、独立した画像検索結果、プロジェクト、branch、履歴検索の data contract は存在しない。

### Invariants

1. Research route では document を縦横の scroll owner にしない。
2. 履歴、回答、desktopソース一覧は、それぞれ明示した内部 scroll container だけがスクロールする。
3. thread header、ソース一覧の開閉操作、composer は回答のスクロールで移動しない。
4. 質問 box の descendant は質問本文だけとし、run status / activity / failure を入れない。
5. run 表示を移動しても SSE、polling、attempt epoch、generation、finalization、cleanup の
   状態機械を変更しない。
6. 96px 以内の auto-follow、`最新の回答へ`、focus 不変、reduced motion を維持する。
7. active thread は URL の `/research/[threadId]` を SSoT とする。
8. 履歴の `limit`、thread navigation pending の first-wins、旧本文保持、操作 lock、anchor
   semantics を維持する。
9. 確定回答と source の SSoT は Server Component が取得した thread detail とし、draft や
   live event から source / missingAspects を合成しない。
10. `sourceRef` は同じ assistant message の content と sources の対応として扱い、thread
    全体で一意だと仮定しない。
11. raw event、内部 metadata、provider payload、chain-of-thought を表示しない。
12. light / dark theme、既存の `--vector-*` token、Vector の typography を維持する。
13. API、Pydantic schema、generated TypeScript types、DB、認証・認可を変更しない。
14. finalization中はstable answer slotを維持し、visible draft、安全な固定placeholder、final answerの
    いずれも存在しないpaint frameを作らない。
15. final answer、source、missing aspectsの同時確定でanswerのvisual anchor、ソース一覧の
    開閉状態、focusを奪わない。

### Non-goals

- 回答本文を置き換える内部view、tab、URL stateを追加すること。
- 画像、動画、独立したリンク検索結果など API にない表示を追加すること。
- 履歴検索、rename、filter、folder、project 機能を追加すること。
- Research 以外の global shell や画面を再設計すること。
- API response shape、SSE event vocabulary、run state machine を変更すること。
- DB schema、Alembic migration、認証・認可を変更すること。
- source を回答間で dedupe したり、新しい関連性を推測すること。
- message pagination や 100 件を超える履歴 pagination を再設計すること。
- composer の Enter 送信、attachment、auto-grow 等の入力仕様を追加すること。
- sidebar preference を localStorage、cookie、global store へ永続化すること。
- 新規 dependency を追加すること。
- 参考画像の配色、文言、製品固有操作をコピーすること。

### Done

本仕様は次をすべて満たしたとき実装 Done とする。

- desktop / compact の必須 viewport で Research 由来の document scroll が発生しない。
- 履歴、回答、desktopソース一覧を独立してスクロールでき、一方のscrollが他方を動かさない。
- desktop では履歴を開閉でき、compact viewport では modal drawer として開閉できる。
- 回答と確定済みソース一覧を同じworkspace内で参照でき、ソース参照のために回答を別画面へ
  切り替える必要がない。
- desktopでは回答右側のcompact panel、狭いviewportでは開閉式の一覧としてソースを表示する。
- composer が thread pane 下部に残り、回答本文を覆わず、inner contentが回答railと同じ最大幅に揃う。
- run status / activity / failure が質問 box 直下かつ box 外へ表示される。
- live draft から DB 確定回答への収束中も assistant 表示領域が消えず、画面切替のような跳躍がない。
- final answer 置換と同時に source が確定しても、answer の表示位置と scrollTop を奪わない。
- mobile safe area と virtual keyboard 表示時にも composer を操作できる。
- navigation pending と live answer の既存契約が退行しない。
- component / integration / Playwright test が本仕様の責務を固定する。
- 実装後に `/check` を完走する。

## Design Direction

デザインの方向性は「Vector の紙面性を残した research workstation」とする。

- app shell は境界線と surface 差で静かに分割し、不要な card nesting を増やさない。
- assistant answer は大きな bubble に閉じ込めず、中央の読み物として表示する。
- user question だけを右寄せの小さな bubble にする。
- teal accent はactive history、sources trigger、primary action、live stateの識別に限定する。
- message rail と composer inner content は同じ最大幅で揃え、wide viewport でも文章を
  不必要に横へ伸ばさない。
- Research workspace は masthead 下の利用可能幅を使い、現在の `max-w-[1280px]` と外周余白に
  閉じ込めない。message / source / composer の読み取り幅は内側で制限する。
- PaperTexture と既存 font token は維持する。

## Data And Display Contract

| UI 領域 | SSoT |
|---|---|
| 履歴 | `PaginatedResearchThreadResponse.items` |
| 質問 | `ResearchUserMessage.content` |
| run status | `ResearchUserMessage.run` + 検証済み live state |
| 確定回答 | `ResearchAssistantMessage.content` |
| inline citation | 同じ assistant message の `content` と `sources[].sourceRef` |
| ソース一覧 | 確定済み assistant message の `sources` |
| 不足事項 | `ResearchAssistantMessage.missingAspects` |
| draft | 検証済み answer delta から構成した既存 live draft |
| composer | 既存 submit / cancel contract |

### 回答領域

回答領域は次だけを表示する。

- user question
- 質問 box 外の run status / current known activity / failure
- temporary assistant draft
- DB 確定済み assistant answer
- inline citation badge / preview
- missing aspects

現在 assistant answer 直下に並ぶ詳細 source card は回答領域から外し、右側または開閉式の
ソース一覧に集約する。
inline citation とその source preview は回答本文の読解に必要なため維持する。

### ソース一覧

ソース一覧は DB 確定済み assistant message の `sources` だけを表示する。回答を別画面へ
切り替えず、回答と同じworkspace内の補助領域として表示する。

1. assistant message の時系列を維持し、message 単位で group 化する。
2. group label は表示順から導出した `回答 1`、`回答 2` とし、API にない意味を付与しない。
3. source の identity と citation 対応は `assistant seq + sourceRef` 単位で扱う。
4. 回答をまたいだ URL / title の dedupe を行わない。
5. internal article は title、publishedAt、`articleId !== null` の場合だけ記事 link を表示する。
6. external URL は title、sourceName、publishedAt、evidenceClaim、外部 link を表示する。
7. active draft の source を予測・先行表示しない。
8. thread 内の source が 0 件ならソース一覧のtriggerをdisabledにし、desktop panelには
   `表示できるソースはありません`を表示する。
9. source title、sourceName、evidenceClaim は長文でも横 overflow を起こさない。
10. desktop panelは回答と独立して縦scrollし、回答のscrollTopを変更しない。
11. compact表示はmodal sheetとし、閉じた後はtriggerへfocusを戻す。

`画像`、`リンク`等の空領域は作らない。internal article と external URL は API の
`ResearchSource` union のままソース一覧へ表示する。

## Layout Contract

### 全体構造

```text
PaperSurface                             # h-dvh / min-h-0 / overflow-hidden / flex-col
├─ ShellMasthead                         # 実外寸を含めてshrink-0
└─ Research route viewport               # flex-1 / min-h-0 / overflow-hidden
   └─ Research workspace                 # h-full / min-h-0 / w-full
      ├─ Desktop history sidebar         # >= 1024px / open時のみlayout幅を持つ
      ├─ Compact history drawer          # < 1024px / modal
      └─ Thread pane                     # min-h-0 / min-w-0 / flex-1
         ├─ Pane header                  # shrink-0
         ├─ Content workspace            # min-h-0 / flex-1
         │  ├─ Answer panel              # internal scroll / flex-1
         │  └─ Desktop sources panel     # >= 1280px / right side / independent scroll
         ├─ Compact sources sheet        # < 1280px / modal / thread選択時のみ
         └─ Composer dock                # shrink-0
```

### Viewport shell

1. Research の `PaperSurface` instance は `h-dvh min-h-0 overflow-hidden flex flex-col` 相当とし、
   masthead と route viewport を同じ column flex に置く。
2. masthead は border を含む実外寸のまま `shrink-0`、route viewport は `flex-1 min-h-0` とする。
   `calc(100dvh - 58px)`等の固定 subtraction で高さを再計算しない。
3. workspace 内部は `h-full` と `min-h-0` を伝播させ、子の internal scroll を成立させる。
4. Research のために global `html` / `body` へ `overflow-hidden` を追加しない。
5. document の `scrollTop` は 0 のままとし、vertical / horizontal scrollbar を発生させない。
6. history list、answer panel、desktop sources panelには scroll chaining を防ぐ
   `overscroll-behavior: contain` 相当を適用する。
7. pane header、sources trigger、composer は answer panel の外側に置く。
8. composer は `position: fixed` で本文へ重ねず、thread pane の非 scroll sibling とする。
9. message rail と composer inner content の最大幅は 860px とし、同じ中心線へ揃える。
10. desktop sources panelは320pxを基準とし、回答railの読み取り幅を860pxより広げない。
11. workspace 全体は利用可能幅を使い、message rail の外側は静かな余白として扱う。

### Desktop: `width >= 1024px`

1. 履歴は左側の inline `aside` として初期表示する。
2. sidebar 幅は 320px とし、thread pane は残り幅を使う。
3. pane header の履歴 toggle で `open ↔ closed` を切り替える。
4. closed 時は sidebar を layout 幅から除き、thread pane を拡張する。
5. sidebar header と「さらに表示」は固定し、thread list だけを縦スクロールする。
6. sidebar を閉じても、pane header には再表示できる toggle を残す。
7. open / closed は現在 mount 中の workspace shell が所有する feature-local client state とする。
8. route cacheから別instanceへ切り替わるthread navigation、shell remount、refresh、hard reload、
   Research外への遷移、再訪をまたぐ維持は要件にしない。shellがremountした場合は初期openへ戻る。
9. state永続化のために`/research/layout.tsx`、route-local provider、localStorage等へ既存境界を
   移動・拡張しない。
10. sidebar を閉じても履歴 DOM を keyboard focus 順に残さない。
11. 開閉 animation は必須としない。追加する場合は 180ms 以内とし、reduced motion では即時に
    切り替える。

### Compact: `width < 1024px`

1. inline sidebar は表示せず、履歴 toggle から左側 modal drawer を開く。
2. drawer の初期状態は closed。
3. drawer 幅は `min(88vw, 320px)`、高さは利用可能な dynamic viewport 高とする。
4. drawer は既存の `components/ui/sheet.tsx` を feature 側から利用し、新規 dependency や
   `components/ui/` の手編集を行わない。
5. close button、Escape、scrim 操作で閉じる。
6. open 中は focus を drawer 内へ留め、背面を操作対象にせず、document をスクロールさせない。
7. close 後は履歴 toggle へ focus を戻す。
8. active thread row を選択した場合は navigation を起こさず drawer だけを閉じる。
9. 別 thread / 新規 thread を選択した場合は navigation 受付時に drawer を閉じる。
10. 「さらに表示」は一覧更新が目的なので commit 後も drawer を開いたままにする。
11. compact へ切り替わった時点で drawer を closed にする。
12. desktop へ戻った場合は、同じ mount 中の直前 desktop open / closed state を復元する。
13. user bubble は最大 88% とし、長文でも horizontal overflow を発生させない。
14. composer の bottom padding は通常余白に `env(safe-area-inset-bottom)` を加味する。
15. textarea の mobile font size は 16px 以上、主要 touch target は 44×44px 以上とする。

### Sources responsive boundary

1. `width >= 1280px`では、確定済みソース一覧をcontent workspace右側のinline `aside`として表示する。
2. inline panelは幅320px、回答panelとは独立した`overflow-y-auto`を持つ。
3. pane headerの`ソース`buttonでinline panelを開閉でき、閉じても同じbuttonから再表示できる。
4. `width < 1280px`ではinline panelを表示せず、同じ`ソース`buttonから右側modal sheetを開く。
5. sheet幅は`min(92vw, 360px)`とし、本文の読み取り幅を縮めない。
6. sheetは既存の`components/ui/sheet.tsx`を利用し、新規dependencyや`components/ui/`の手編集を
   行わない。
7. source countは既存`ResearchAssistantMessage.sources`の件数からだけ導出してbuttonへ表示できる。
8. source 0件ではbuttonをdisabledにし、APIにない空の候補や関連情報を表示しない。
9. viewport境界をまたぐ場合、inline panelとsheetを同時表示しない。compactへ移るとsheetはclosed、
   desktopへ戻るとinline panelは初期openとする。
10. panel/sheetを開閉してもanswer scrollTop、stable answer slot、live controller、composer input、focusを
    意図せず再初期化しない。

## Thread Pane Contract

### Pane header

1 行目に次を置く。

- 履歴 toggle
- thread title。`/research` では `新しいリサーチ`
- thread が存在する場合だけ delete action

title は1行で truncateし、長い title が履歴toggle、sources button、delete actionを押し出さない。

threadが存在する場合だけpane headerに`ソース`buttonを表示する。buttonはsource総数をAPI dataから
表示でき、`aria-expanded`と表示対象の`aria-controls`を持つ。`/research`の空状態ではbuttonと
空のsource panelを描画せず、開始案内とcomposerを表示する。

### URL contract

ソース一覧はrouteや内部viewではないため、`view` queryを持たない。

- `/research`と`/research/[threadId]`は既存の`limit`だけを維持する。
- 旧実装中に導入された`view` queryは表示状態として解釈せず、回答を通常表示する。
- thread / new / moreのanchor `href`へ`view`を引き継がない。
- ソース一覧の開閉はfeature-local UI stateであり、browser history、route navigation、API fetch、
  run cancelを開始しない。
- thread navigation、delete、composerの既存pending lockは維持する。
- navigation pending中も履歴toggleとsources buttonはview-only操作として利用できる。

### Answer panel

会話 turn の表示構造を次に固定する。

```text
user turn group
├─ user question bubble                   # question text only
├─ run status rail                        # bubble外 / assistant railに左揃え
│  ├─ queued / stage / failure label
│  └─ current known activity（存在時）
└─ temporary assistant answer（存在時）

final assistant answer
├─ answer content + inline citation
└─ missing aspects（存在時）
```

1. user question bubble の descendant は question text だけとする。
2. queued / running だけでなく failed / cancelled の固定文言も bubble 外へ置く。
3. run status rail は bubble 直後の sibling とし、assistant answer と同じ message rail へ
   左揃えする。
4. stage label は1行、activity は最大2行を基本とし、長い query は折り返して横幅を広げない。
5. activity の出現・消失で status rail の高さが変わることは許容するが、question bubble の
   寸法を変えない。
6. draft は既存仕様どおり、最初の表示可能文字が到着した場合だけ status rail 後の
   temporary assistant 領域へ表示する。
7. completed 後は DB 確定済み assistant answer へ収束し、question bubble 内に完了状態を
   残さない。
8. assistant answer は大きな bubble で囲わず、本文中心の表示を維持する。
9. missing aspects は関連する final assistant answer の直後に残す。
10. 詳細 source card はanswer panelから除き、inline citation previewと独立したソース一覧だけを残す。

既存 live scroll contract:

- 最下部から96px以内だけ answer update に自動追従する。
- 96pxを超えて上を読んでいる場合は位置を奪わず、`最新の回答へ`を表示する。
- stage / activity だけの更新で answer content revision を進めない。
- streaming、attempt切替、terminal、final answer置換で focus を移動しない。
- reduced motion では spinner animation と smooth scroll を抑制する。

### Finalization presentation continuity

live draft から DB 確定回答への収束は、別画面への遷移ではなく、同じ会話 turn 内の表示内容の
確定として扱う。API response shape、SSE event vocabulary、DB transaction は変更しない。

1. active run の各 turn は、最初の draft 表示から final answer 表示まで同一の stable answer slot を持つ。
   run status rail、answer slot、answer先頭anchorのDOM上の所有者を、`running`から`completed`への
   state変更だけを理由に一括で破棄しない。
2. 通常のcompleted経路では、DB確定済みassistant messageを描画できるまで、現在見えているdraftを
   同じslot内に保持し、`回答を確定しています…`相当のstatusをslot外のstatus railで表示する。
3. DB確定回答の到着時は、保持中のdraftを同じslot内のfinal answerへ1回のcommitで置換する。
   draftとfinal answerを同時に見せず、両方が存在しないpaint frameも作らない。
4. EventSourceがterminal event前に`CLOSED`となりpollingへfallbackする経路では、未完了draftを
   final answerのように残さない既存の安全契約を維持する。ただしstable answer slot自体は畳まず、
   完了を断定しない固定placeholderを表示する。pollingでcompletedを確認した後はfinalizing表示へ
   収束させ、DB確定回答が描画可能になるまで同じslotを維持する。
5. stable answer slotは常に`draft`、安全な固定placeholder、`final answer`のいずれか1つを表示する。
   stage / activityの有無や一時的なtransport状態によってslot全体の高さを0にしない。
6. draftは一時表示のままであり、確定回答、source、missing aspectsへ昇格・合成しない。final answer、
   source、missing aspectsのSSoTは引き続きDB確定済みassistant messageとする。
7. 通常のanswer deltaに対する96px auto-followは維持するが、final answer置換は通常deltaとは別の
   scroll eventとして扱う。置換時にcontainer全体の`scrollHeight`末尾へ自動scrollしない。
8. final answer置換の前後でanswer先頭anchorのviewport位置、または同等のvisual anchorを維持する。
   ユーザーが末尾から96pxを超えて上を読んでいる場合はscrollTopを維持し、final answerがviewport外なら
   `最新の回答へ`を表示する。末尾付近の場合もfinal answerの連続位置へ合わせ、source panel末尾を
   追従先にしない。
9. final answerと同じRSC commitでsourceやmissing aspectsが追加されても、それらの後続高さを理由に
   answer先頭をviewport外へ押し出さない。補足情報と右側ソース一覧の追加後もanswer panelの
   visual anchorを維持する。
10. finalizationのためだけの待ち時間、擬似streaming、APIにないprogressは追加しない。transitionを
    付ける場合はopacity / transformに限定し、`prefers-reduced-motion`では無効化する。

browser testにおけるcontinuityの許容差はbrowser rounding分の1 CSS px以内とする。ユーザーが
末尾から96pxを超えている場合はscrollTop、末尾付近の場合はanswer先頭anchorのviewport座標を測る。
content縮小でscroll rangeがclampされる場合も、anchor補正後の座標を合格判定に使う。

### Sources list

1. desktop inline panelはanswer panelとは別の`overflow-y-auto` containerを持つ。
2. compact sheet内のsource listはsheet headerの下だけを縦scrollする。
3. source groupとsource itemはData And Display Contractに従い、過剰なcard nestingを避けた
   compactな縦リストとして表示する。
4. source linkは内部記事も外部URLも既存のnavigation semanticsを維持する。
5. source itemはcitation番号、title、利用可能なsourceName / publishedAtを優先し、
   evidenceClaimは最大3行を基本としてpanel幅を広げない。
6. live draftの更新だけではsource listの内容やscrollTopを変更しない。
7. final assistant messageによるsources更新は同じlist内だけで反映し、answer panelのscrollTop、
   visual anchor、focus、panel/sheetの開閉状態を変更しない。

### Composer dock

1. thread pane の最下部に常時表示する。
2. sources panel/sheetの開閉にかかわらず同じcomposer instanceを表示する。
3. answer panelまたはdesktop sources panelをスクロールしてもcomposerのviewport座標を変えない。
4. answer panelへのoverlayにせず、thread paneの非scroll siblingとする。
5. inner content は message rail と同じ最大860pxに揃える。
6. content との境界は border または surface 差で示し、本文を覆う gradient overlay は使わない。
7. active run 中は既存どおり textarea を disabled にし、停止 button を表示する。
8. navigation pending、submit pending、cancel pending の既存 lock を維持する。
9. textarea の rows、maxLength、native multiline、送信操作を本仕様では変更しない。
10. composer 高が変わる場合はcontent workspaceの利用可能高だけを縮め、末尾contentを覆わない。
11. compact viewport では safe area の上に表示する。
12. virtual keyboard 表示時は dynamic viewport の利用可能高へ追随し、keyboard 背後へ隠さない。

## State Model

```text
viewport mode
├─ desktop (>= 1024px)
│  └─ sidebar: open | closed
└─ compact (< 1024px)
   └─ drawer: closed | open

thread state
├─ empty (/research)
│  └─ sources control: absent
└─ selected (/research/[threadId])
   └─ sources presentation
      ├─ wide (>= 1280px): inline open | closed
      └─ narrow (< 1280px): sheet closed | open

navigation state
├─ idle
└─ pending(target)                     # existing first-wins contract
   └─ route commit -> idle

run presentation
├─ queued       -> bubble外に「待機中」
├─ running      -> bubble外にstage / known activity
├─ completed    -> final assistant answer
└─ failed       -> bubble外に安全な固定文言
```

sidebar、drawer、sources presentationは表示stateであり、backend run stateを変更しない。

## Navigation Compatibility

`frontend/specs/research-thread-navigation-pending.md` の次の契約を維持する。

- thread / new / more の same-tab navigation は最初の1件だけを採用する。
- commit 前に active thread を切り替えない。
- pending target、旧本文、detail overlay、ARIA status を表示する。
- pending 中は Research navigation、delete、submit / cancel を lock する。
- shell navigation、citation / source link、履歴 toggle は利用可能とする。
- 履歴toggleとsources buttonはnavigation pending中もview-only操作として利用できる。
- modifier click / middle click は anchor の browser default へ渡す。
- active thread row は redundant navigation を開始しない。
- navigation commit / boundary unmount 後に pending state を残さない。

compact drawer から navigation を始める場合、drawer を閉じた後も pane 側の既存 overlay と
live region が pending target を通知する。

## Live UI Compatibility

`frontend/specs/agent-research-live-ui-slice.md` の次の契約を維持する。

- live controller の identity は run ID。
- 同一 run ID の再描画で state / EventSource を初期化しない。
- thread / run 切替で旧 resource を cleanup し、backend run を暗黙に cancel しない。
- draft は一時表示であり、DB 確定回答へ昇格させない。
- finalizing / failed / cancelled の収束と安全な固定文言を維持する。
- raw event と未知 field を UI へ流さない。
- completion announcer、ARIA、focus、reduced motion を維持する。
- 96px auto-follow と `最新の回答へ`を維持する。
- transportの安全契約を維持しながら、draft / placeholder / final answerのstable slotを切らさない。
- final answer置換を通常deltaと分け、answerのvisual anchorを維持する。

本仕様が変更するのはrun status / activity / failureのDOM配置、ソース一覧の開閉に依存しない
live announcement ownership、finalization時のpresentation continuityとscroll ownershipである。
通知語彙・通知頻度・live state、transport、永続化契約は変更しない。

```text
旧: user question bubble
      ├─ question
      └─ run status / activity

新: user turn group
      ├─ user question bubble
      │  └─ question
      └─ run status / activity / failure
```

## Accessibility And Keyboard

### History

- desktop history は `aside`、一覧は `nav aria-label="リサーチ履歴"` とする。
- toggle は native `button` とし、`aria-controls="research-history"`、`aria-expanded` を持つ。
- accessible name は状態に応じて `履歴を開く` / `履歴を閉じる` とする。
- active thread は `aria-current="page"` を維持する。
- navigation pending の `aria-busy` / `aria-disabled` と activation guard を維持する。

### Compact drawer

- `role="dialog"`、`aria-modal="true"`、visible title を持つ。
- open 時は close button または drawer heading へ focus を移す。
- Tab / Shift+Tab は drawer 内へ留める。
- Escape、scrim、close button で閉じ、toggle へ focus を戻す。
- open 中は背面を accessibility tree と操作対象から除外する。

### Sources list

- pane headerのtriggerはnative `button`とし、`aria-expanded`、`aria-controls`を持つ。
- desktop inline listは`aside aria-label="ソース"`とし、headingと件数を表示する。
- compact sheetは`role="dialog" aria-modal="true"`、visible titleを持つ。
- sheet open中はfocusをsheet内へ留め、Escape / scrim / close buttonで閉じる。
- close後はsources triggerへfocusを戻す。
- source itemのlinkは判別可能なaccessible nameを持ち、外部linkは新しいtabで開く既存semanticsを
  維持する。
- trigger、link、close buttonにはfocus-visible stateを必ず表示する。

### Live status

- ソース一覧を開閉しても通知契約を失わないよう、単一の安定したscreen-reader用announcerを
  thread pane内に置く。
- announcer は queued / stage / generation開始 / finalizing / failure / cancelled / completion の
  大きな状態変化だけを `role="status" aria-live="polite" aria-atomic="true"` で通知する。
- visible な `ActiveRunStatus` / `LiveAnswerDraft` は同じ文言を重複通知せず、live announcement の
  ownership を外側の announcer に一本化する。
- activity 本文と draft delta を live region 内へ入れない。
- sources panel/sheetの開閉だけでは現在状態を再通知しない。
- spinner は `aria-hidden="true"`。
- 色、animation、spinner だけで状態を表現しない。
- update、sources開閉、final answer置換でkeyboard focusを奪わない。

### Composer

- textarea の accessible name は `質問`を維持する。
- textarea に意味のある `name` を設定する。
- submit / cancel は visible label または同等の accessible name を持つ。
- disabled 理由を placeholder だけに依存させない。
- mobile で自動 focus しない。

## Frontend Structure

### 既存責務

| Component | 維持する責務 |
|---|---|
| `ResearchWorkspace` | server data を workspace shell へ compose |
| `ResearchNavigationBoundary` | pending target、first-wins navigation、overlay / live status |
| `ResearchSidebar` | 履歴 header、thread list、さらに表示 |
| `ResearchThreadView` | message order、user / assistant / run status の配置 |
| `ResearchThreadLiveBoundary` | live controller 接続、answer scroller、auto-follow |
| `ResearchLiveAnnouncer` | active runの安定した状態通知（thread pane内の単一owner） |
| `ResearchComposer` | input、submit / cancel、operation lock |
| `ActiveRunStatus` | stage / known activity の presentation |

### 追加・分離候補

実装時の component 名は責務に合わせて確定するが、少なくとも次を分離する。

```text
features/research/components/
  ResearchWorkspaceShell.tsx    # viewport mode、sidebar/drawer、pane layout
  ResearchSourcesPanel.tsx      # wide inline panel / narrow sheet + source grouping
  ResearchAnswerSlot.tsx        # draft / placeholder / finalのstable slot + visual anchor
  ResearchRunStateAnnouncer.tsx # thread pane内の単一live status owner（名称は既存統合可）
```

- feature-local component は `components/ui/` を手編集しない。
- global store、app-wide provider、custom CSS file を追加しない。
- route pageは`searchParams.view`を表示stateとしてparseせず、既存`limit`だけを扱う。
- client state が必要な shell / sources panel / drawer だけを client boundary にし、server data の取得を
  client fetch へ移さない。
- `ResearchNavigationLink`は既存`limit`だけでthread / moreの実anchor `href`を構築し、`view`を
  引き継がない。
- sources panel/sheetの開閉でanswer live subtreeをunmountしない。
- sidebar の desktop / drawer presentation で thread navigation logic を二重実装しない。

### Expected file changes

主要な変更候補:

```text
frontend/src/app/(protected)/research/page.tsx
frontend/src/app/(protected)/research/[threadId]/page.tsx
frontend/src/features/research/index.ts
frontend/src/features/research/schemas/research.ts
frontend/src/features/research/components/ResearchWorkspace.tsx
frontend/src/features/research/components/ResearchNavigationBoundary.tsx
frontend/src/features/research/components/ResearchSidebar.tsx
frontend/src/features/research/components/ResearchNavigationLink.tsx
frontend/src/features/research/components/ResearchThreadView.tsx
frontend/src/features/research/components/ResearchThreadLiveBoundary.tsx
frontend/src/features/research/components/ResearchLiveAnnouncer.tsx
frontend/src/features/research/components/ResearchComposer.tsx
frontend/src/features/research/components/ActiveRunStatus.tsx
frontend/src/features/research/components/ResearchWorkspaceShell.tsx       # new candidate
frontend/src/features/research/components/ResearchSourcesPanel.tsx         # new candidate
frontend/src/features/research/components/ResearchAnswerSlot.tsx           # new/merge candidate
frontend/src/features/research/components/ResearchRunStateAnnouncer.tsx    # new/merge candidate
frontend/src/features/research/components/*.test.tsx
frontend/e2e/research.spec.ts
backend/scripts/seed_e2e_research.py                                     # test-support only
```

既存 local/CI test-support の`backend/scripts/seed_e2e_research.py`は必須で拡張し、history、answer、
desktop sourcesの各scroll containerが必ずoverflowする件数・長文・sourceを固定する。変更は既存cleanup対象を
含むE2E dataの追加に限定し、product API / schema / migrationを変更しない。

変更しない領域:

```text
backend/app/schemas/
backend/app/agent/ のproduct code
backend/alembic/
frontend/src/types/*.gen.ts
frontend/src/types/client/
frontend/src/types/core/
frontend/src/components/ui/
frontend/src/app/globals.css
frontend/package.json
認証・認可
```

## Test Ownership

### URL compatibility

- route pageとworkspaceが`view`をselected stateとして受け取らない。
- thread / new / moreの実anchor `href`へ`view`を追加・継承しない。
- 既存`limit`をthread / more navigationで維持する。
- `?view=sources`等の旧URLをdirect loadしても通常の回答workspaceを表示し、別画面を描画しない。

### Workspace component

- desktop sidebar の初期 open、close、reopen。
- toggle の `aria-expanded` / `aria-controls` / accessible name。
- closed sidebar が keyboard focus 対象に残らない。
- compact drawer の initial closed、open、Escape / close、focus return。
- thread / new 選択で drawer を閉じ、more では維持する。
- viewport mode 切替時の state 分離。
- shell remount後はdesktop sidebarが初期openへ戻る。

### Sources list

- tablist / tabpanelを描画せず、回答とsources listを同じworkspace内に表示する。
- threadがない場合はsources trigger / panel / sheetを描画しない。
- 1280px以上ではinline `aside`を初期表示し、triggerでclose / reopenできる。
- 1279px以下ではinline asideを描画せず、triggerからmodal sheetをopen / closeできる。
- responsive境界変更でinline asideとsheetを同時表示しない。
- panel / sheetの開閉でcomposer input、answer scrollTop、live subtree identityを維持する。
- source を assistant message 単位で group 化し、回答間で dedupe しない。
- source 0 件の empty state。
- draft から source を表示しない。
- source countをAPI dataからだけ導出する。
- source list表示中のanswer deltaでもanswer scrollerの通常auto-follow契約を維持し、sources scrollTop、
  live controller identity、composer入力を変更しない。
- thread pane内の単一announcerだけが状態変化を通知し、sources開閉で重複通知しない。

### Message placement / live regression

現行の「progressをuser card内へ置く」testを、次の保証へ置換する。

- question bubble の descendant に stage / activity / failure が存在しない。
- run status rail は bubble 直後、temporary assistant draft より前の sibling。
- stage / activity 更新後も bubble の text content は question 本文だけ。
- stage / activity 更新だけでは answer content revision を進めない。
- draft、finalization、failure、completion announcer の既存 test を維持する。
- 96px / 97px、burst coalescing、`最新の回答へ`、reduced motion の既存 test を維持する。

### Finalization continuity

- visible draftがある通常completed経路で、RSC finalをcommitするまで同じstable answer slot内のdraftを
  維持し、draftからfinal answerへ置換する途中に空の表示状態を挟まない。
- `draft -> completed/finalizing -> DB final`の各renderで、stable answer slotのDOM identityと
  answer先頭anchorを維持する。
- DB final、sources、missing aspectsを同時に返すfixtureでも、draftとfinalを同時表示せず、
  final answerの先頭位置とanswer scrollTopを不自然に跳躍させない。
- terminal event前の`EventSource.CLOSED -> polling -> completed`ではdraft本文を非表示にする一方、
  安全な固定placeholderを持つstable answer slotをfinal描画まで維持する。
- final answer置換時にcontainerの絶対末尾へ`scrollTo`せず、96px以内・97px以上の両条件で定義済みの
  visual anchor / scrollTop / `最新の回答へ`契約に収束する。
- final source更新はsources panel/sheetの開閉状態を変えず、answer panelのscrollTop、visual anchor、
  focusを変更しない。
- component testは同期後の最終DOMだけでなく、finalization途中の各renderを観測し、
  `draft`、`placeholder`、`final answer`のいずれかが常に1つだけ見えることをassertする。

### Composer / safe area

- composerがcontent workspaceのsiblingであり、overlay / `position: fixed`ではないことを保証する。
- composerのbottom paddingに通常余白と`env(safe-area-inset-bottom)`を組み合わせた宣言がある。
- composer inner contentがanswer railと同じ`max-width: 860px`、同じ中心線を持つ。
- sources panel/sheet開閉で同じtextarea instanceと入力値を維持する。
- active run / navigation pendingの既存disabled契約を維持する。

### Playwright responsive contract

既存の `user` project 内で `page.setViewportSize()` を使い、全 suite を viewport 数だけ
複製しない。必須 viewport は次とする。

```text
390 × 844       # phone
767 × 900       # masthead mobile boundary直前
768 × 900       # masthead desktop boundary
1023 × 900      # compact boundary直前
1024 × 768      # desktop boundary
1440 × 900      # desktop
```

各 scroll test は対象 container 自身について次を前提 assert する。

- history contract は history list の `scrollHeight > clientHeight`。
- answer contract は answer panel の `scrollHeight > clientHeight`。
- desktop sources contract はinline panelの`scrollHeight > clientHeight`。
- 対象の前提が成立しない test を pass と扱わない。

必須 E2E:

1. document `scrollY === 0`。
2. `documentElement.scrollHeight <= clientHeight`。
3. `documentElement.scrollWidth <= clientWidth`。
4. documentへwheel / programmatic scrollを試みても`window.scrollY === 0`。
5. answer panelをscrollしてもheader / sources trigger / composerのbounding boxが変わらない。
6. 最終 content が composer に隠れない。
7. history、answer、desktop sourcesをそれぞれ実際にscrollし、他のcontainer / composer / documentが
   動かない。
8. 1023px 以下では inline sidebar がなく、drawer が初期 closed。
9. drawer open 中は document / background が scroll せず、close 後に focus が戻る。
10. 1024px 以上では sidebar が初期 open で、close / reopen できる。
11. sidebar close で pane が拡張し、composer の bottom 座標は変わらない。
12. 1280px以上ではsources inline panelを初期表示し、close / reopenできる。
13. 1279px以下ではinline panelを表示せず、sources sheetをopen / Escape / closeでき、focusが戻る。
14. sources panel/sheetを開閉してもcomposerが同じ位置・同じ入力値を維持する。
15. compact drawer から thread を選択した pending 中も、旧本文、target status、operation lock を
    維持し、commit 後に新 thread へ切り替わる。
16. 長い title、question、activity、source title で横 overflow が発生しない。
17. thread / new / more anchorの`href`が`view`を含まず、modifier / middle clickでも現在の
    sources開閉stateを変更しない。
18. thread navigation pending中もsources panel/sheetを開閉でき、遅延commit後に旧threadの
    sourcesや開閉操作が新threadのanswer/live stateを巻き戻さない。
19. fake SSE terminalから実際の`router.refresh()` / RSC final commitまでanimation frameを観測し、
    visibleなdraft、placeholder、final answerのいずれかが常に存在し、draftとfinalが同時表示されない。
20. final answer、sources、missing aspectsが同時にcommitされても、answer先頭anchorのbounding boxと
    answer panelのscrollTopが定義済み許容範囲を超えて跳躍せず、sources開閉stateも変わらない。
21. terminal event前にEventSourceを`CLOSED`にしたpolling fallbackでも、final表示までanswer slotが
    消えず、未完了draftがfinal answerとして残らない。

non-zero `safe-area-inset-bottom` と実 virtual keyboard の完全な挙動は desktop Chromium だけでは
保証できないため、CSS contract の component 確認に加えて iOS Safari / PWA の手動確認項目とする。

手動確認の合格条件:

1. home indicatorがある端末でtextareaへfocusし、virtual keyboardを表示する。
2. textarea、send / stop controlがkeyboardとsafe areaの上に見え、操作できる。
3. keyboard表示中もdocument scrollが発生せず、answer panelだけをscrollできる。
4. keyboardを閉じるとcomposerが元のpane下端位置へ戻り、contentを覆わない。

## Implementation Constraints

- 実装開始前と検証後に`git status --short`と対象fileのdiffを比較する。
- 本仕様と無関係なdirty / untracked fileを編集、削除、stageしない。
- 対象fileに既存差分がある場合は、その差分を正本候補として読んだ上で限定的に統合し、
  `git checkout`、`git reset`等の復元操作で消さない。
- formatter、type generation、Next.js command等が無関係なfileを変更した場合は自動的に復元せず、
  原因と差分を確認して作業を止める。
- `next-env.d.ts`、generated types、`components/ui/`を本UI実装の都合で手編集しない。
- fixture拡張は既存E2E userの決定的dataとcleanupだけに限定し、product dataへ適用しない。

## Implementation Order

1. route viewport と workspace の高さ境界を修正し、document scroll を除去する。
2. workspace shell と desktop sidebar / compact drawer を追加する。
3. 回答右側のdesktop sources panelとcompact sources sheetを追加する。
4. run status / failure を question bubble 外へ移す。
5. stable answer slotとfinal answer置換時のvisual anchor / scroll ownershipを実装する。
6. composer inner rail、safe area、answer/sources scroll ownershipを整える。
7. component test と決定的な responsive / finalization E2E fixture / test を追加する。
8. 既存 navigation pending / live answer test を含め `/check` を実行する。

各 step の完了時に、その step が所有する test を通す。API / schema / dependency の変更が必要に
見えた場合は本仕様の範囲を超えているため、実装を止めて別判断とする。

## Verification

実装後は少なくとも次を実行する。

- Research component / integration tests
- 既存 live reducer / controller / scroll tests
- 既存 navigation pending E2E
- 本仕様の responsive Playwright E2E
- 1440×900、1024×768、1023×900、768×900、767×900、390×844 の visual 確認
- iOS Safari または PWA 相当で safe area / virtual keyboard の手動確認
- `/check`

API、Pydantic schema、generated types、DB を変更しないため、通常は `/api-contract`、
`/gen-types`、`/migration`を実行しない。実装中にこれらが必要になった場合は scope divergence と
して停止する。

## Done Checklist

- [ ] Research route に document scroll がない。
- [ ] history / answer / desktop sources が独立した scroll owner を持つ。
- [ ] desktop sidebar を開閉でき、閉じても再表示できる。
- [ ] compact viewport では履歴が modal drawer になる。
- [ ] 回答を別画面へ切り替えるtab / view / URL stateを作っていない。
- [ ] thread / new / moreのURLが`limit`を維持し、`view`を引き継がない。
- [ ] desktop inline sourcesとcompact sources sheetを同時表示しない。
- [ ] answer live subtreeとcomposer inputがsources開閉で失われない。
- [ ] question bubble に question text 以外を入れていない。
- [ ] run status / activity / failure が bubble 外かつ draft 前にある。
- [ ] draft / placeholder / final answerのstable slotがfinalization中に消えない。
- [ ] CLOSED fallbackで未完了draftを隠しつつ、final描画まで安全なplaceholderを維持する。
- [ ] final answer置換でanswer先頭anchorとscrollTopが跳躍しない。
- [ ] final source更新がsources開閉state、answer scrollTop、focusへ干渉しない。
- [ ] composer がanswer panelを覆わずpane下部に残る。
- [ ] composer inner contentがanswer railと同じ最大860px・中心線に揃う。
- [ ] mobile safe area と virtual keyboard を確認した。
- [ ] navigation pending の first-wins / lock / overlay が維持されている。
- [ ] live controller / finalization / announcer / auto-follow が維持されている。
- [ ] desktop / compact の component test と Playwright が green。
- [ ] `/check` が green。
