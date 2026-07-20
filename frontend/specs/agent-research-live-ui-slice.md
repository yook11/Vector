# Agent Research live UI slice 仕様

> 後続契約更新: `backend/specs/agent-input-safety-gate-slice.md` はterminal statusへ
> `policy_blocked`を追加し、assistant messageではないpolicy noticeへ収束させる。

Status: Implemented — 2026-07-13

親仕様: `../../backend/specs/agent-answer-streaming-sse.md`

前提slice:

- `../../backend/specs/agent-live-stream-transport-slice.md`
- `../../backend/specs/agent-attempt-epoch-fencing-token-slice.md`
- `../../backend/specs/agent-sse-backend-bff-slice.md`
- `../../backend/specs/agent-live-event-producer-wiring-slice.md`
- `../../backend/specs/agent-direct-answer-deltas-slice.md`
- `../../backend/specs/agent-evidence-answer-draft-deltas-slice.md`

後続slice:

- Operational verification

## Positioning

本sliceは、実装済みのResearch run SSEをfrontendで消費し、現在の工程・activityと生成途中の回答を
Research thread上へ表示する。

生成途中の回答は一時的なdraftであり、確定回答の正本ではない。SSEは速く変化を伝える補助経路、
既存pollingとPostgres上のthread detailは正しさへ収束する経路として扱う。

本書では次の9点を確定済みとする。

1. live通信、状態遷移、進捗表示、draft表示、会話内配置の責任を分離する。
2. draftは実行中のuser message直後に、一時的なassistant messageとして表示する。
3. attemptEpoch、generation、Stream IDを別の責任として比較し、retry、再実行、再配送でdraftを混在させない。
4. SSEを即時表示、2秒pollingを正しさと回復の経路として並行稼働させ、45秒closeを通常の再接続として扱う。
5. completedでは表示中draftを維持してDB確定結果を待ち、failed / cancelledではdraftを回答として残さない。
6. 現在表示中のactive runだけをlive購読し、thread移動ではfrontend購読だけを終了してbackend runを継続する。
7. SSEの生dataをruntime parserで既知eventへ投影し、壊れたdataやStream IDをreducerへ入れない。
8. 状態変化だけをscreen readerへ通知し、draft本文のdeltaを逐次読み上げず、userのscrollとfocusを奪わない。
9. parser、reducer、controller、component、scroll、operationalの責任に合わせて保証testを分ける。

本slice内の設計判断は完了している。実装中に本書のinvariantを変更する必要が生じた場合は、暗黙に変更せず
仕様へ戻って判断する。

## Work definition

### Problem

backendは`stage`、`activity`、`answer.delta`、`answer.reset`、`terminal`をSSEで配信できるが、
現在のResearch UIはrun detailをpollingして工程と一部activityを表示するだけで、回答本文を生成途中に
表示できない。

既存`ActiveRunStatus`へSSE接続、polling、attempt / generation判定、draft連結、terminal処理、表示を
すべて追加すると、通信・状態判断・presentationが1 componentへ集中する。重要な境界規則がReactの
副作用や見た目と結合し、再配送、retry、切断を独立してテストしにくくなる。

またdraftを既存の進捗文へ混ぜると、「今何をしているか」と「回答として読める文章」の区別が曖昧に
なる。空の回答枠、内部JSON、citation marker、接続断ごとの消失は、停止や回答破損に見えるため避ける
必要がある。

### Evidence

以下は2026-07-13時点に確認した移行元の証拠である。

- `frontend/src/features/research/components/ActiveRunStatus.tsx`
  - Client Componentとしてrun detailを2秒間隔でpollingする。
  - status、progressStage、recentEventsを同一component内で保持する。
  - terminal状態、401 / 403 / 404では`router.refresh()`へ収束する。
  - tab非表示中はpollingを止め、再表示時に即時pollingする。
- `frontend/src/features/research/components/ResearchThreadView.tsx`
  - user message、assistant message、run statusの会話内配置を所有する。
  - active run statusは現在user message card内へ表示される。
  - 確定assistant messageは`CitedAnswerContent`、sources、missing aspectsを描画する。
- `frontend/src/features/research/components/ActiveRunStatus.test.tsx`
  - 初期status、polling更新、既知activity、terminal refresh、visibility lifecycleを既存テストが所有する。
- `frontend/src/app/api/research/runs/[runId]/events/route.ts`
  - browserと同一originのSSE BFF routeが実装済みである。
- `backend/specs/agent-answer-streaming-sse.md`
  - event vocabulary、attemptEpoch / generation、terminal、reconnect、polling fallbackの親契約を定義する。
- `backend/specs/agent-evidence-answer-draft-deltas-slice.md`
  - 同一generationの重複resetを破棄境界にしないconsumer testを本Research UI sliceへ委譲している。

既存実装は移行元を示す証拠であり、本sliceの責任境界そのものではない。

### Invariants

#### Responsibility boundary

1. EventSource / pollingのlifecycle、live eventによる状態遷移、進捗表示、draft表示、会話内配置を
   それぞれ独立した責任として扱う。
2. `ActiveRunStatus`へSSE解析、attempt / generation reducer、draft本文の所有を集約しない。
3. attempt / generationの境界規則は、React componentやEventSourceへ依存しない純粋なreducerで判定する。
4. EventSourceとpollingの開始・停止・cleanupはlive state hookまたは同等のcontroller境界が所有する。
5. presentation componentは、受け取った表示用stateから見た目を組み立て、cursorやevent decodeを知らない。
6. `ResearchThreadView`は会話内の配置を所有し、SSE protocolの詳細を知らない。
7. frontendのglobal state libraryを追加せず、active run単位のlocal stateとして管理する。

#### Draft presentation

8. draftは実行中のuser message直後に、一時的なassistant messageとして表示する。
9. user message card内の進捗と、assistant側のdraft本文を同じ表示領域へ混ぜない。
10. 表示可能な最初の文字を受信するまでは進捗だけを表示し、空のassistant draft枠を表示しない。
11. draftが存在するときは、確定回答と区別できるラベルとして正確に`回答を生成中…`と表示する。
12. draft本文は通常の可読性を維持し、生成中であることを本文全体の低contrastや点滅だけで表現しない。
13. frontendはruntime parserが投影した`answer.delta.text`だけを通常のReact textとしてdraftへ渡し、raw JSON、
    envelope、他field、内部event名、attemptEpoch、generation、provider metadataを表示へ流さない。
14. JSON構造と完全な`[[N]]` citation markerを`answer.delta.text`へ入れない責任はDirect / Evidence backend
    producerが所有する。frontendは同じ増分filterを二重実装せず、citation link、source card、missing aspectsは
    Postgresから取得する確定assistant messageだけが表示する。実producerからUIまでの非露出はOperational testが
    所有する。
15. retryまたはattempt切替では、古いdraftと新しいdraftを連結せず、古いdraftを破棄してから新しい
    表示revisionを適用する。詳細な判定規則は次の設計項目で固定する。
16. 200 streamの終了後に同じEventSourceが`CONNECTING`となる通常・一時的な再接続ではdraftを消さない。
    `CLOSED`となるlive継続不能とは区別し、接続障害をrun失敗とは扱わない。
17. 正常terminal後は、DBの確定回答を再取得する間は現在表示中のdraftを維持し、ラベルを正確に
    `回答を確定しています…`へ変更する。取得後に確定assistant messageへ置き換え、draft自体を確定回答へ
    昇格させない。
18. failed / cancelled terminalを検知した時点で`draftText`を空にし`draftMode = suppressed`としてDOMから
    非表示にし、確定回答として残さずDBのrun状態へ収束させる。
19. userが過去の本文を読んでいるときに、delta受信だけを理由としてscroll位置を強制的に奪わない。

#### State transition

20. `attemptEpoch`は「どのworker実行に属するeventか」、`generation`は「同じattempt内のどの表示revisionか」、
    Redis Stream IDは「どこまで配送・適用したか」を表し、3つの責任を混同しない。
21. 初期stateにcurrent attemptがない場合、最初に受信した検証済み公開eventの正の`attemptEpoch`を現在値として
    pinする。`attempt.started`の到着だけを初期化の前提にしない。
22. 現在より大きい`attemptEpoch`を6種類のどの公開eventで観測しても、event適用前にdraft、draftMode、
    generation、stage、current activity、activity history、その他attempt-local progressをすべて初期化し、
    新attemptへ切り替えてからそのeventを適用する。
23. 現在と同じ`attemptEpoch`の重複`attempt.started`ではdraftを破棄しない。現在より小さいepochのeventは
    Stream IDが新しくても適用しない。
24. `generation`は`answer.delta`と`answer.reset`だけが更新する。現在generationがない場合は、最初の有効な
    resetまたはdeltaの正のgenerationを現在値とする。
25. `answer.reset`は接続やrunをresetするeventではなく、現在draftを破棄して指定generationの表示revisionへ
    切り替える制御eventとする。reset自体をユーザーへ表示しない。
26. 現在より大きいgenerationのresetでは、event適用前に旧draftを破棄して新generationへ切り替える。
27. resetが喪失しても、現在より大きいgenerationのdeltaをimplicit resetとして扱い、旧draftを破棄してから
    そのdeltaを適用する。正しさをreset entryの到着だけに依存させない。
28. 現在と同じgenerationの重複resetはno-opとし、すでに表示している同generationの正しいdraftを
    再破棄しない。現在より小さいgenerationのreset / deltaは適用しない。
29. 現在と同じattemptEpoch・generationの新しいdeltaだけをdraft末尾へ1回追加する。
30. 処理済みRedis Stream IDのeventを再処理しない。Stream IDはreplayの再開・重複排除にだけ使い、
    attemptやgenerationの帰属判定には使わない。表示上無視したvalidなstale epoch / generation eventも
    `lastProcessedEventId`までは前進させる。
31. terminalを適用したstateはそのrunのlive更新に対する終端stateとし、その後に届くstage、activity、
    answer.delta、answer.reset、attempt.startedを表示stateへ適用しない。
32. completed terminalではdraftをDB再取得中の一時表示として維持するが、draftを確定回答へ昇格させない。
    failed / cancelled terminalの具体的な表示タイミングは後続のterminal収束設計で固定する。

#### Connection lifecycle and polling fallback

33. active runでは同一runに対するEventSourceを最大1 instanceだけ開き、既存の2秒pollingを並行して維持する。
34. SSEはstage、activity、draftを低遅延で反映する経路、pollingはDB上のterminal検知とlive継続不能時の
    progress fallback経路とする。どちらの障害もrun状態を変更しない。
35. polling responseはdraftを復元せず、`lastProcessedEventId`を進めない。より大きい有効epochを受けた場合だけ
    attempt切替としてdraft、generation、stage、activityを初期化してよいが、pollingからdraftを生成・復元しない。
    `recentEvents`は既知activityとしてruntime検証するが、Redis Listは順序保証外かつattempt帰属保証外である。
36. polling responseは次の順で適用する。(1) completed / failedはDB正本としてepochに依存せずterminalを最優先で
    適用する。(2) active statusは`queued < running`の単調mergeであり、遅延queued responseでdemoteしない。
    (3) stage / activityは正のsafe integerの`attemptEpoch`だけを対象にする。current epochがnullならattempt reset後に
    stageを採用し、同一epochでは共有の単調stage mergeで厳密な前進だけを採用し、小さいepochは無視する。大きいepoch
    ではattempt reset後にstageを採用し、そのresponseのList activityは採用しない。epochが欠落、0、不正なら
    stage / activityをmergeしない。stageは`live` / `reconnecting`でも前進可能だが、List activityは初回採用時の
    `connecting`（SSE未受理）または`polling-only`、同一epochでは`polling-only`だけで採用する。queued stateで
    検証済み非terminal SSE eventを受理した場合は、DB acquire後のeventであるため`runStatus`をrunningへ単調に進める。
37. heartbeatは10秒間隔のcomment frameでJavaScript eventとしてdispatchされないため、最後の公開eventからの
    経過時間だけを根拠にsilence timeoutを発火しない。
38. backendの45秒max ageによる200 stream終了は通常動作とする。同じEventSourceの`CONNECTING`と
    `retry: 1000`による自動再接続に任せ、別instanceを並行作成しない。
39. `CONNECTING`中はdraft、currentAttemptEpoch、currentGeneration、lastProcessedEventId、進捗表示を維持し、
    初期loading表示へ戻さない。
40. EventSourceが再びopenした場合は同じlocal stateのまま`live`へ戻り、未適用eventだけをreducerへ渡す。
41. EventSourceが`CLOSED`になった場合はlive継続不能として`polling-only`へ移し、`draftText`を空にして
    `draftMode = suppressed`とし、本文をDOMから取り除く。後続completedでも復活させない。本sliceではmanual
    SSE retryや永続cursorを追加しない。
42. SSE terminalはparser出力をcontrollerが直接消費せず、reducerがStream IDと現在epochを検査して受理した
    terminal transitionだけがfinalization開始条件になる。pollingのcompleted / failedはDB正本としてepochに
    依存せず受理する。どちらを先に観測してもfinalizationを1回だけ実行する。
43. terminal eventを喪失してもpollingのterminal検知、または45秒close後の再接続preflightによる終了から
    DBの最終状態へ収束する。
44. `connecting`、`live`、`reconnecting`等の内部接続状態を、一時的な遷移ごとにユーザー向け文言として
    表示しない。通常再接続で`回答を生成中…`をちらつかせない。

#### Terminal and final result convergence

45. terminalはDB commit後の終端通知であり、確定回答本文そのものではない。SSE draft、terminal payload、
    polling responseから確定assistant message、sources、missing aspectsを合成しない。
46. completed terminalまたはpolling completedを検知したら、表示中のdraftを維持して`finalizing`へ遷移し、
    ラベルを`回答を確定しています…`へ変更してthread detailを再取得する。
47. `polling-only`移行時に不完全として非表示にしたdraftは、後からpolling completedを検知しても再表示しない。
    draft本文がない状態で`回答を確定しています…`だけを表示し、DB結果を待つ。
48. thread detail再取得が失敗または反映されない間もdraftを確定回答へ昇格させず、citation、sources、
    missing aspectsを表示しない。
49. completed後のthread detail再取得は2秒から開始し、4秒、8秒、最大10秒のbackoffで、active run componentが
    同じrun IDのまま残る間だけ再試行する。component unmountまたはrun ID変更でtimerを回収する。
50. failed terminalまたはpolling failedを検知したら`draftText`を空にして`draftMode = suppressed`とし、
    thread detail再取得中は既存の安全なerror code投影による固定文言だけを表示する。
51. `status = failed AND errorCode = cancelled`はキャンセルとして扱い、draftを非表示にして
    `キャンセルしました`と表示する。cancel前のdraftを復活・確定表示しない。
52. failed / cancelled後のthread detail再取得が失敗または反映されなくても、draftを再表示せず、安全な固定文言を
    維持しながら同じbackoffで再取得する。
53. reducerは受理済みterminalをtransition resultとしてcontrollerへ返すか、`terminal: null`からの受理済みstate
    遷移を明示する。controllerはこの結果とpolling terminalだけをfinalization入口とし、stale epoch / replay
    terminalではconnection close、timer cleanup、refresh、表示切替を行わない。
54. finalization開始後に受信したdelta、reset、stage、activity、attempt.startedを適用せず、DB再取得の成功だけが
    `terminal`表示へ遷移できる。

#### Navigation, run identity, and visibility lifecycle

55. live controllerのidentityはthread IDではなくrun IDとする。同じthread内でもrun IDが変われば別の実行として
    古いcontrollerをcleanupしてからstateを初期化する。
56. 同じrun IDのprops更新やReact再描画だけではdraft、attemptEpoch、generation、Stream ID、EventSourceを
    初期化しない。
57. frontendは現在表示中のthreadに属するactive runだけをlive購読する。表示対象から外れたrunのEventSource、
    polling、finalization timer、listener、pending request、一時draft stateを回収する。
58. thread移動、run ID変更、component unmountはfrontendの購読終了であり、backend runのcancel操作ではない。
    worker、provider生成、DB commitは継続し、停止は既存の明示的cancel操作だけが要求する。
59. cleanup済みrunの遅延polling response、EventSource callback、refresh timerが、現在表示中の別runのstateを
    更新しない。pending requestのabortとrun identity確認を併用する。
60. 以前のthreadへ戻った場合はServer ComponentのDB状態を入口とする。activeなら新しいcontrollerを開始し、
    terminalならDBの確定結果を表示する。以前のReact draft stateの保存・復元を前提にしない。
61. active runへの再入場時、cursorなしreplayが保持suffixの途中から始まることを許容する。最初のeventがmarker /
    resetなしのgeneration 2 deltaでもepochとgenerationをpinしてdraft表示を開始してよい。このdraftは常に
    `回答を生成中…`の一時表示であり完全性を主張せず、確定回答へ昇格させず、terminal後にDB回答へ完全置換する。
62. documentがhiddenになった場合は通常pollingとfinalization retry timerを休止するが、同じEventSource instanceと
    local draft stateは維持する。visibility変更だけを理由にEventSourceをclose / recreateしない。
63. documentがvisibleへ戻ったら、同じrun IDであることを確認して即時pollingまたはfinalization refreshを1回行う。
    EventSourceが`OPEN`なら継続、`CONNECTING`なら自動再接続を待ち、`CLOSED`ならpolling-onlyを維持する。
64. hidden中にterminalを受信した場合もfinalization guardを適用し、EventSourceを閉じる。DB結果がまだ画面へ
    反映されていなければ、visible復帰時にrefreshを即時再開する。
65. 複数tab間でEventSource、draft、cursorを共有しない。各tabは独立したconsumerとし、接続上限を超えたtabは
    既存SSE契約に従ってpolling-onlyへ劣化する。

#### Runtime parsing and Stream ID integrity

66. EventSourceの`MessageEvent`、event name、`data`文字列、`lastEventId`を直接reducerへ渡さず、純粋な
    runtime parserで検証済みfrontend eventへ変換する。
67. frontendが処理するSSE eventは`attempt.started`、`stage`、`activity`、`answer.delta`、
    `answer.reset`、`terminal`の6種類だけとする。未知eventをstateやUIへraw投影しない。
68. parserはJSON objectであることとevent固有のrequired fieldを検証し、既知fieldとcanonical IDを`BigInt`
    pairへ変換した値だけから新しいtyped eventを構築する。受信object自体や余剰fieldをreducerへ渡さない。
69. `attemptEpoch`と`generation`は`Number.isSafeInteger`を満たす1以上の整数だけを受理する。stage、terminal
    status、activity discriminatorは既知allowlistだけを受理し、delta textは空でないstringだけを受理する。
70. activityはnested `activity` fieldとcamelCase属性を検証し、既存`AnswerProgressEvent`に対応する既知typeだけを
    presentationへ渡す。未知activityや壊れたactivityは他のlive stateへ影響させない。
71. terminalの未知statusは適用せずpollingへ委ねる。failedで`errorCode`なし、または未知codeはterminalを捨てず
    generic failureへ正規化する。completedに`errorCode`があってもcompletedを受理し、codeを表示へ使わない。
72. 未知event、JSON parse失敗、object以外、field型不正、未知stage / activityは、そのeventだけを無視して
    stateを変更せず接続を継続する。検証失敗をrun失敗へ変換しない。
73. 全公開eventはcanonical Redis Stream IDを必須とする。parserがmissingまたはinvalid IDをprotocol integrity
    failureとして返した場合、controllerはeventを適用せずEventSourceを閉じ、draftをsuppressedにして
    polling-onlyへ移す。
74. Redis Stream IDは`<milliseconds>-<sequence>`のcanonical decimal表現とし、各partはunsigned 64-bit以下、
    全体は41文字以下とする。符号、空part、追加separator、不要な先頭zeroを許可しない。
75. Stream IDは文字列の辞書順やJavaScript `number`で比較せず、検証後の2 partを`BigInt`へ変換し、
    milliseconds、sequenceの順に数値pairとして比較する。
76. parserはcanonical形式の検証と`BigInt` pair化だけを所有し、reducerが`lastProcessedEventId`との大小比較と
    適用可否を単独で所有する。同じ、または小さいIDはreplay / out-of-orderとして再処理せず、大きいvalid eventは
    domain上無視する場合も処理済みIDを前進させる。controllerはIDの大小比較を行わない。
77. answer textを含む受信dataは通常のReact textとしてだけ描画し、HTMLとして解釈しない。raw payload、
    回答本文、activity本文、run ID、user IDをvalidation log、metric label、例外messageへ含めない。
78. runtime parserの失敗は固定された低cardinality reasonでだけ診断可能にし、payload内容や未知type文字列を
    そのまま観測値へ使用しない。

#### Accessibility and scroll ownership

79. draft本文を`aria-live` regionに入れず、deltaごとに本文全体をscreen readerへ再通知しない。
80. `回答を生成中…`、`回答を確定しています…`、安全な失敗・キャンセル文言だけを各表示境界の
    `role="status"`または`aria-live="polite"`で大きな状態変化ごとに1回通知する。`回答が完了しました`は
    active componentのunmountをまたいで残る専用`ResearchLiveAnnouncer`だけが所有する。
81. `ResearchLiveAnnouncer`は同じclient sessionでactiveだったrunがDB確定表示へ遷移した場合だけ完了を1回通知し、
    completed threadの初回表示・再訪では通知しない。activity本文とdraft deltaをannouncerや既存`role="status"`
    の内側へ入れない。thread ID変更では観測済みrunと通知済みrunを初期化し、別threadで観測したrunを再訪時の
    完了通知へ使用しない。
82. live draft regionは生成中・確定中を`aria-busy`で表現し、spinnerは`aria-hidden="true"`とする。状態を色、
    animation、spinnerだけで伝えない。
83. delta、reset、attempt切替、terminal、DB確定回答への置換でkeyboard focusを自動移動しない。
84. `prefers-reduced-motion`ではspinner animationとsmooth scrollを抑制する。
85. thread scroll containerの最下部から96px以内にいる場合だけ、draft追加後に最下部へ自動追従する。
    thresholdはmodule定数として所有し、event contractへ含めない。
86. userが最下部から96pxを超えて上へscrollした場合は自動追従を止め、後続deltaでscroll位置を奪わない。
87. 自動追従停止中に新しい表示内容が届いた場合は`最新の回答へ`buttonを表示し、押下時だけ最下部へ移動して
    自動追従を再開する。
88. retry、attempt切替、terminal後の確定回答置換でも古いdraftは契約どおり破棄するが、userが上を読んでいる
    場合に強制的に最下部へ移動しない。

#### Test ownership

89. runtime parser testはevent allowlist、field投影、malformed data、errorCode正規化、canonical Stream IDの
    uint64 pair変換を所有する。
90. reducer testはStream ID比較、attemptEpoch、generation、明示・暗黙reset、重複reset、replay、
    accepted terminal、terminal後のstate不変を所有する。
    所有する。Evidence sliceから委譲された同一generation重複reset testをここに置く。
91. controller testはEventSource、polling、timer、visibility、run identity、cleanup、terminal race、
    finalization retryをfake dependencyで所有する。
92. component / scroll testはdraft配置、表示文言、非表示情報、ARIA、focus不変、自動追従、`最新の回答へ`、
    reduced motionを所有する。
93. 本sliceはfrontend内のfake EventSource統合までを所有し、実Redis、backend / BFF通過、proxy buffering、
    idle timeout、複数instance、実ブラウザ45秒再接続、Redis停止はOperational verification sliceへ委譲する。
94. 既存backend transport / SSE / BFFのframe、認証、認可testをfrontendへ複製せず、frontend consumerが
    所有する入力境界と表示結果だけを追加で検証する。

### Non-goals

- backend event vocabulary、SSE endpoint、BFF、Redis Stream、attempt epoch、generation producerを変更すること。
- draftをPostgres、localStorage、sessionStorageへ永続化すること。
- draftからcitation link、sources、missing aspectsを先行表示すること。
- draftを編集可能なdocumentや確定messageとして扱うこと。
- thread history、navigation、composer、確定assistant messageの情報設計を再設計すること。
- global state library、新規dependency、WebSocket、frontend acknowledgmentを追加すること。
- 本書の未決事項を、実装時の暗黙判断で確定すること。

### Done

本sliceは次をすべて満たしたときDoneとする。

- 通信、状態遷移、進捗表示、draft表示、会話内配置が上記の責任境界に分離される。
- 最初の表示可能文字の前には進捗だけが表示され、到着後に`回答を生成中…`付きのassistant draftが現れる。
- frontendがraw JSON、内部metadata、sources、missing aspectsを表示へ流さず、backend producerとOperational testが
  citation markerを含まないend-to-end表示を保証する。
- retry / attempt切替で旧draftと新draftが混在しない。
- 大きいepochでdraft / generationだけでなくstage / activity / 全attempt-local progressを初期化し、最初の
  stage / activity / delta / terminal eventを旧表示なしで適用する。
- resetと大きいgenerationのdeltaが定義どおりdraft境界になり、古いepoch / generationを表示へ適用しない。
- 同一epochの重複marker、同一generationの重複reset、同一Stream IDのreplayで正しいdraftを消去・重複しない。
- stale epoch / replay terminalではfinalizationを開始せず、polling terminalはepoch非依存で受理する。
- terminal適用後のlive eventと遅延queued / running polling responseでfinalizing stateが巻き戻らない。
- queued開始後に検証済み非terminal SSE eventを受理するとrunningへ進み、`待機中`を残さずdraftを表示できる。
- EventSourceと2秒pollingが同一active runで並行し、pollingがSSE由来のdraftを復元せず、Stream IDを進めない。
  新しい有効epochのpollだけはattempt-local stateをresetしてstageを採用する。
- 初回有効epochの`connecting`（SSE未受理）またはpolling-onlyでは検証済み`recentEvents`から最新の関連activityを
  表示できる。同一epochのactivityはpolling-onlyだけで置換し、live / reconnectingでは遅いpolling activityで
  巻き戻さない。Listは順序保証外でattempt帰属を保証しない。
- active pollingが即時、成功後2秒、失敗後4 / 8 / 10秒で非重複実行され、abort後にtimerを残さない。
- 45秒closeと`CONNECTING`中はdraftを維持し、同じEventSourceが約1秒後に差分から再開する。
- `CLOSED` / invalid Stream IDではdraftTextを消去してdraftModeをsuppressedにし、DOMから本文を除き、
  completed後にも復活させない。
- terminalとpollingの終端検知が競合してもfinalizationを1回だけ実行し、DBの確定結果へ置き換わる。
- completed後は`回答を確定しています…`を表示し、再取得失敗中もdraftを確定回答へ昇格させない。
- failed / cancelled検知時はdraftを即座に非表示にし、安全な固定文言だけを表示する。
- thread detailが反映されない間は2秒から最大10秒のbackoffでrefreshを再試行し、unmount時にtimerを残さない。
- failed / cancelled runのdraftが確定回答として残らない。
- thread / run切替で旧EventSource、timer、request、callbackを回収し、旧runのeventが新runへ混ざらない。
- React StrictModeを含め、同一runで同時にopen / connectingなEventSourceを最大1つに保つ。
- thread移動ではfrontend購読だけを終了し、backend runを暗黙にcancelしない。
- hidden中はpollingを休止して同じEventSourceとdraftを維持し、visible復帰時に即時DB確認を行う。
- 生のSSE dataがruntime parserを通らずreducerへ到達せず、未知・壊れたeventが表示stateを変更しない。
- Stream IDをcanonical uint64 pairとして検証・比較し、桁数差を含むreplayを二重適用しない。
- missing / invalid Stream IDでは不完全なliveを継続せずpolling-onlyへ安全に劣化する。
- pollingが401 / 403 / 404を返した場合はEventSource、draft、timer、request、visibility listenerを回収して
  refreshし、componentが残ってもlive / pollingを再開しない。
- parserがStream IDを検証・pair化し、reducerだけがlastProcessedEventIdを比較し、controllerは比較しない。
- cursorなし再入場のsuffix draftを一時表示としてだけ扱い、terminal後にDB回答へ完全置換する。
- draft本文をdeltaごとに読み上げず、状態変化だけをpoliteに通知し、focusを奪わない。
- 専用announcerが同じthread内のactiveからDB確定表示への遷移時だけ完了を1回通知し、初回completed表示、
  thread移動後の再訪では通知しない。
- 最下部から96px以内だけ自動追従し、userが上を読んでいる間は`最新の回答へ`で明示的に復帰できる。
- reduced motion設定でspinnerとsmooth scrollを抑制する。
- burst deltaでscroll処理を重複予約せず、確定回答置換時にdraftとfinalを同時表示しない。
- parser、reducer、controller、component、scrollの必須testが本書の条件と1対1で対応する。
- frontendのlint、format、typecheck、unit / integration testが通る。
- reducer、connection lifecycle、component表示の保証をそれぞれ適切なテスト境界で固定する。

## Responsibility model

```text
ResearchThreadView
  |-- user / assistant messageの配置
  |-- ActiveRunStatusの配置
  `-- LiveAnswerDraftの配置

useResearchRunLiveState または同等のcontroller
  |-- EventSource lifecycle
  |-- polling fallback lifecycle
  |-- runtime parserの検証済みeventだけをreducerへdispatch
  `-- 表示componentへlive stateを返す

research run SSE runtime parser
  |-- event name / data / lastEventIdを検証
  |-- 既知fieldだけをtyped eventへ投影
  `-- reducerから生のMessageEventを隔離

research run live reducer
  |-- Stream ID / attemptEpoch / generation境界
  |-- stage / activity / draftMode / terminal state
  |-- accepted terminal transition
  `-- 副作用を持たない状態遷移

ActiveRunStatus
  `-- 工程・activity・接続状態のpresentation

LiveAnswerDraft
  |-- `回答を生成中…`ラベル
  |-- 表示可能な自然文draft
  `-- finalizing表示

ResearchLiveAnnouncer
  |-- thread view内の安定したclient boundary
  |-- activeからDB確定表示への遷移を追跡
  `-- 完了時だけ1回polite通知
```

component名とfile配置は実装前のExpected file changesで最終確定するが、上記の責任境界は維持する。

## Fixed design decisions

### 1. State ownership and component boundary

- live connectionはactive runごとに1つのcontroller境界が所有する。
- reducerは現在stateと検証済みeventから次stateを返す純粋関数とする。
- `ActiveRunStatus`はpolling・EventSource・draft本文を抱える巨大componentにしない。
- `LiveAnswerDraft`は表示だけを担当し、attemptEpoch、generation、cursorを比較しない。
- `ResearchThreadView`はdraftをuser message cardの内部ではなく、その直後のassistant側領域へ配置する。

### 2. Draft placement and presentation

表示の基本形は次とする。

```text
user message
  `-- 工程・activity

temporary assistant message
  |-- 回答を生成中…
  `-- 表示可能な回答draft
```

- 最初の表示可能文字まではtemporary assistant messageを作らない。
- draft出現後は`回答を生成中…`を表示する。
- retry / attempt切替の瞬間に旧本文と新本文を同時表示しない。
- SSE再接続中も同じ表示revisionのdraftを維持する。
- completed terminal後の再取得中はdraftを維持し、DBの確定assistant messageと原子的に入れ替える。
- failed / cancelledへ収束した場合はdraftを確定表示へ転用しない。

### 3. attemptEpoch / generation / reset / replay / terminal state transition

stateの中心は、少なくとも次を持つ。

```text
currentAttemptEpoch: positive integer | null
currentGeneration: positive integer | null
progressStage: planning | retrieving | synthesizing | null
currentActivity: KnownActivity | null
draftText: string
draftMode: empty | visible | suppressed
lastProcessedEventId: Parsed Redis Stream ID | null
hasAcceptedSseEvent: boolean
terminal: completed | failed | null
```

connection modeはcontrollerが所有する。attempt-local表示stateの基本関係は次とする。

```text
run
`-- attemptEpoch
    `-- generation
        `-- answer.delta
```

- attemptEpochが増える: worker実行全体の切替としてdraft、generation、stage、activityをすべて初期化する。
- generationが増える: 同じattempt内の表示revision切替としてdraftを破棄する。
- Stream IDが進む: 同じeventを二重適用せず配送を継続する。epoch / generation境界には使わない。

draft modeの操作は次に固定する。

- 初期state、新attempt、大きいgeneration reset: `draftText = ""`、`draftMode = empty`
- 有効なdelta適用後: `draftMode = visible`
- 同一generation reset: draftText / draftModeを変更しない
- `CLOSED`、invalid Stream ID、failed / cancelled: `draftText = ""`、`draftMode = suppressed`
- completed: visible draftはfinalizing中だけ維持し、suppressed draftは復活させない

`answer.reset { generation: G }`の意味は、「現在draftを破棄し、generation Gを受け入れる状態へ
切り替える」である。処理は次に固定する。

| 受信event | 現在値との関係 | reducerの処理 |
|---|---|---|
| 任意の公開event | epochが大きい | draft / generation / stage / activityを初期化し、新epochへ切り替えてからeventを処理 |
| 任意の公開event | epochが小さい | eventを無視 |
| `attempt.started` | epochが同じ | 重複markerとしてdraftを維持 |
| `answer.reset` | generationが大きい | draftを破棄し、新generationへ切替 |
| `answer.reset` | generationが同じ | 重複resetとしてno-op |
| `answer.reset` | generationが小さい | eventを無視 |
| `answer.delta` | generationが大きい | implicit resetとしてdraftを破棄し、新generationへ切り替えてdeltaを追加 |
| `answer.delta` | generationが同じ | 新しいStream IDならdraft末尾へ1回追加 |
| `answer.delta` | generationが小さい | eventを無視 |
| 任意のevent | Stream IDが処理済み | replayとして再処理しない |
| terminal後のlive event | 関係なし | 終端stateを維持し、表示へ適用しない |

大きいepochの境界では`draftText = ""`、`draftMode = empty`、`currentGeneration = null`、
`progressStage = null`、`currentActivity = null`にしてから新eventを適用する。activity履歴はstateへ保持せず、
現在表示する最新activityだけを所有する。
最初の新epoch eventがstage / activity / delta / terminalのどれでも、旧attemptの表示を残さない。

reducerの判定順序とtransition resultは次とする。

1. terminal済みstateへの後続live eventを拒否する。
2. reducerが`lastProcessedEventId`とStream IDを比較し、同じ・小さいIDを拒否する。
3. 大きいvalid IDは、epoch / generationで表示上無視するeventでも`lastProcessedEventId`を前進させる。
4. attemptEpochの大小を判定し、大きい場合はevent適用前に全attempt-local stateを初期化する。
5. 小さいattemptEpochは表示へ適用せず、accepted terminalも返さない。
6. answer eventではgenerationの大小を判定し、大きい場合はevent適用前にdraft境界を適用する。
7. 同一generationのresetはno-opとする。
8. 有効なstage / activity / deltaを適用する。
9. 現在epochのterminalだけを終端stateとして適用し、transition resultへ`acceptedTerminal`を返す。

reducerは`{ state, acceptedTerminal }`または同値の明示的transition resultを返す。controllerは
`acceptedTerminal`だけをSSE由来finalizationの入口とし、parsed terminal自体を直接監視しない。

### 4. SSE / polling connection state machine

SSEとpollingは排他的な代替手段ではない。active runでは両方を開始し、役割を次に分ける。

| 経路 | 主な責任 | 更新してよいstate |
|---|---|---|
| SSE | 低遅延の工程・activity・draft・terminal | reducerが所有するlive state |
| polling | DB上のrun status確認、live継続不能時のprogress fallback | terminal判定、許可時のprogressと検証済みrecentEvents |
| thread detail再取得 | 最終結果への収束 | 確定assistant message、sources、missing aspects、失敗状態 |

pollingはmount / run ID変更 / visible復帰時に即時実行する。成功responseの処理完了後に2秒待って次を実行し、
失敗時は4秒、8秒、以後最大10秒へbackoffする。前回requestの完了前に次を開始せず、abort / unmount / run ID
変更後に次timerを予約しない。401 / 403 / 404ではEventSource、draft、timer、request、visibility listenerを
回収し、controllerを恒久停止してから`router.refresh()`へ収束する。refresh後もcomponentが残る場合にmanual
SSE retryやpolling再開を行わない。

SSEが`live`でもpolling自体は止めずcompleted / failedの検知を続ける。poll responseはterminal、active status、
stage / activityの順で統合する。terminalはepochにかかわらずDB正本として最優先でfinalizationへ渡す。active statusは
`queued < running`の単調mergeとし、stageが不採用でもrunning responseで進め、遅延queued responseで戻さない。

stage / activityは正のsafe integer `attemptEpoch`だけを対象にする。stage rank
`null < planning < retrieving < synthesizing`はreducerの共有関数でSSEとpollの両方に適用し、同一attemptの
遅延SSE / pollで後退しない。表示へ反映しないSSE eventも、validで新しいStream IDならcursorを前進させる。
polling `recentEvents`はSSE activityと同じallowlist / camelCase field規則で検証し、unknown、schema違反、
snake_case payloadを捨てる。Listは順序保証外かつattempt帰属保証外であり、planning / nullでは最新
`question.resolved`、retrievingでは最新検索activity、synthesizingではactivityなしを選ぶ。

| poll epoch と current epoch | stage | List activity | attempt-local state |
|---|---|---|---|
| epoch欠落・0・不正 | mergeしない | mergeしない | 変更しない |
| currentがnull | reset後に採用 | `connecting`でSSE未受理、または`polling-only`だけで採用 | draft / generation / stage / activityを初期化 |
| 小さい | 無視 | 無視 | 変更しない |
| 同じ | 厳密に前進する場合だけ採用 | `polling-only`だけで採用 | 変更しない |
| 大きい | reset後に採用 | 同じresponseでは採用しない | draft / generation / stage / activityを初期化 |

pollingはdraftを生成・復元せず、`lastProcessedEventId`を進めない。新epoch resetはSSE
`attempt.started`と同じattempt境界を作るが、Redis Listのactivityをその境界の根拠にしない。

| connection state | polling stage | polling terminal |
|---|---|---|
| `connecting` / `live` / `reconnecting` / `polling-only` | 有効epochのattempt-aware mergeで前進時だけ適用 | 受理 |
| `finalizing` / `terminal` | 無視 | 既存finalizationを重複実行しない |

connection stateは次を持つ。

| state | 意味 | draftの扱い |
|---|---|---|
| `connecting` | 最初のopenを待っている | 既存draftがなければ進捗だけを表示 |
| `live` | EventSourceがopenしている | SSE eventをreducerへ適用 |
| `reconnecting` | 同じEventSourceが`CONNECTING`で自動再接続中 | draftと境界stateを維持 |
| `polling-only` | EventSourceが`CLOSED`でlive継続不能 | draftTextを空にしdraftModeをsuppressedにする |
| `finalizing` | terminalを検知しDB結果を再取得中 | completedならdraftを一時維持 |
| `terminal` | DBの最終状態へ収束済み | 確定結果または失敗表示を使用 |

基本遷移は次とする。

```text
connecting --open--> live
live --200 stream EOF / error + CONNECTING--> reconnecting
reconnecting --open--> live
connecting / reconnecting --error + CLOSED--> polling-only
connecting / live / reconnecting / polling-only --terminal event or polling terminal--> finalizing
finalizing --thread detail resolved--> terminal
```

45秒max ageは`live -> reconnecting -> live`の定常経路である。次を保証する。

- `retry: 1000`を受け取った同じEventSourceの自動再接続へ任せる。
- `CONNECTING`中に新しいEventSourceを作らない。
- draft、attemptEpoch、generation、lastProcessedEventIdを初期化しない。
- loading表示へ戻さず、接続状態の一時的変化をユーザーへ逐次通知しない。
- 再接続後はLast-Event-IDの後から届く未適用eventだけを処理する。

heartbeat commentは接続維持用であり、JavaScriptから受信時刻を観測できない。公開eventが一定時間ないことは
正常な検索・provider待機でも起こるため、独自silence timerでEventSourceをclose / recreateしない。
livenessとterminal喪失の回復は並行pollingが担う。
画面表示中かつ通信正常なら、stageは次回成功poll（通常約2秒）で回復する。hidden中はpollingを休止し、失敗時は
backoffするため、無条件の回復時間上限は保証しない。

EventSourceが`CLOSED`になった場合は、204 / 409 / 429 / 503等の具体statusをnative EventSourceから
分岐材料として取得せず、同じrunをpolling-onlyへ固定する。manual reconnect用query cursor、別EventSourceの
定期生成、draft永続化は追加しない。controllerはreducerへdraft suppression actionを渡し、`draftText = ""`、
`draftMode = suppressed`としてDOMから本文を除く。completedを後から検知してもsuppressed draftは復活させない。

terminal eventとpolling terminalは競合し得るため、controllerはidempotentなfinalization guardを持つ。
reducerの`acceptedTerminal`またはpolling terminalの最初の終端検知だけがEventSource close、polling cleanup、
thread detail再取得を開始し、stale / replay terminalと後着した終端信号は同じ処理を実行しない。

### 5. Terminal and final result presentation

terminalはDB commit後に送られるため、completedを検知した時点で確定結果はPostgresに存在する。ただし
terminal payloadは確定回答を運ばず、draftも確定結果の正本ではない。最終表示は必ずServer Componentが
再取得したthread detailから構築する。

終端状態ごとの表示は次に固定する。

| 検知した状態 | draft | 再取得中の表示 | 再取得成功後 |
|---|---|---|---|
| completed | 現在表示中なら維持 | `回答を確定しています…` | DBの確定assistant messageへ置換 |
| completed after polling-only | 非表示のdraftを復活させない | `回答を確定しています…` | DBの確定assistant messageを表示 |
| failed | 即座に非表示 | error codeの安全な固定文言 | DBのfailed run表示へ置換 |
| failed + cancelled | 即座に非表示 | `キャンセルしました` | DBのcancelled表示へ置換 |

failed時の一時表示は既存の投影を再利用する。

| errorCode | 表示文言 |
|---|---|
| `cancelled` | `キャンセルしました` |
| `enqueue_failed` | `実行キューに投入できませんでした` |
| `stale` | `時間切れになりました` |
| `generation_unavailable` | `回答を生成できませんでした` |
| その他・不明 | `回答を生成できませんでした` |

finalizationは次の単一経路へ収束させる。

```text
reducer accepted SSE terminal --+
                                +--> finalization guard --> EventSource close
DB polling terminal ------------+                         --> active polling stop
                                                          --> presentation切替
                                                          --> router.refresh()
```

最初の`router.refresh()`後も同じactive run componentが残る場合は、確定thread detailがまだ画面へ反映されて
いないものとして、2秒、4秒、8秒、以後最大10秒間隔でrefreshを再試行する。`router.refresh()`の戻り値を
成功ackとして扱わず、Server Componentの再描画により同じrunのactive componentがunmountされることを
完了条件とする。

completedの再取得中は表示中draftを維持するが、`CLOSED`後のpolling-only移行ですでに不完全として非表示に
したdraftを復活させない。failed / cancelledでは再取得の成否にかかわらずdraftを非表示のまま維持する。

finalization用retry timerはactive run polling timerとは別に所有する。終端検知時にEventSourceと通常pollingを
停止し、component unmount、run ID変更、確定thread detail反映のいずれかでfinalization timerを回収する。

### 6. Navigation, run identity, and tab visibility

live controllerはrun IDをidentity keyとする。thread IDだけでは、同じthreadで後続質問から新しいrunが
作られた場合に旧draftと新runを区別できないため使用しない。

run IDによるlifecycleは次に固定する。

| 変化 | controllerの処理 | backend run |
|---|---|---|
| 同じrun IDで再描画 | EventSourceとlocal stateを維持 | 継続 |
| 別threadへ移動 | 現在runのfrontend resourceをcleanup | 継続 |
| 同じthreadでrun ID変更 | 旧runをcleanup後、新run stateを初期化 | 旧runの状態に従って継続 |
| component unmount | EventSource、timer、request、listenerをcleanup | 継続 |
| 明示的cancel | frontend購読を終端収束へ移す | cancel APIが停止を要求 |

thread移動またはunmount時にcleanupするfrontend resourceは次とする。

- EventSource instance
- active run polling timer
- finalization refresh timer
- visibility listener
- pending polling requestのAbortController
- old runのcallbackを無効化するlifecycle identity
- memory上のdraft、epoch、generation、Stream ID、connection state

cleanupはcancel APIを呼ばない。画面を見ていることとrun実行のlifecycleを結合せず、browser navigation、
誤操作、通信断、tab closeをユーザーのcancel意思として解釈しない。

```text
表示中thread A
  |-- SSE / polling / draftあり
  `-- backend run A実行中

thread Bへ移動
  |-- frontendのrun A購読をcleanup
  |-- thread Bのactive runだけを購読
  `-- backend run Aは生成とDB保存を継続
```

old runの非同期処理はcleanupと競合し得る。pending fetchはabortし、それでも完了したresponseやqueue済み
EventSource callbackは、処理開始時に捕捉したrun ID / lifecycle identityが現在値と一致する場合だけstateへ
適用する。

以前のthreadへ戻った場合は、保存済みfrontend draftを復元しない。Server Componentが返すDB状態から開始する。

| DB状態 | 再入場時の動作 |
|---|---|
| completed | DBの確定assistant messageを表示 |
| failed / cancelled | DBの終端表示を使用 |
| queued / running | 新しいEventSourceとpollingを開始 |
| queued / runningでSSE継続不能 | polling-onlyで最終結果を待つ |

active runのRedis eventが保持されていれば新controllerがdraftを再構築できるが、cursorなしでは保持suffixが
全文か途中かを判別できない。marker / resetなしのgeneration 2 delta等から開始しても、最初のvalid eventで
epoch / generationをpinして表示を始めてよい。この表示は常に`回答を生成中…`の一時draftであり完全性を
主張しない。MAXLEN / TTLを超えた完全復元より、terminal後のDB最終結果への完全置換を優先する。

tab visibilityはthread移動と区別する。同じcomponentとrun IDが残るため、hidden中はpolling requestと
finalization retryを休止する一方、同じEventSource instance、draft、epoch、generation、Stream IDは維持する。
これによりnative EventSource内部のLast-Event-IDを失わず、新しいmanual cursor契約を追加しない。

visible復帰時は次の順で処理する。

1. controllerのrun IDが現在表示中runと一致することを確認する。
2. activeなら即時polling、finalizingなら即時refreshを1回行う。
3. EventSourceが`OPEN`ならそのまま使用する。
4. `CONNECTING`なら同じinstanceの自動再接続を待つ。
5. `CLOSED`なら新しいEventSourceを作らずpolling-onlyを維持する。

複数tabは独立consumerとして扱い、BroadcastChannel、SharedWorker、localStorage等でlive stateやconnectionを
共有しない。接続上限による拒否は回答生成を失敗させず、そのtabだけをpolling-onlyへ劣化させる。

### 7. SSE runtime parser and Stream ID comparison

runtime parserはEventSourceとreducerの間に置く純粋な境界とする。parserは形式検証と既知field投影を所有し、
reducer stateとの新旧比較は行わない。

```text
EventSource MessageEvent
  |-- event name
  |-- data string
  `-- lastEventId
          |
          v
runtime parser
  |-- protocol metadata validation
  |-- JSON object / field validation
  `-- known field projection
          |
          v
typed frontend live event
  |-- canonical Stream ID
  `-- parsed BigInt pair
          |
          v
reducer
```

parserはSSE event nameをdiscriminatorとして使い、data内に別の`type` fieldがあることを要求しない。
`activity`だけはnested domain payloadの`activity.type`を既存safe activity unionのdiscriminatorとして使う。

eventごとの入力契約は次とする。

| event | 必須field | 検証 |
|---|---|---|
| `attempt.started` | `attemptEpoch` | safeな正整数 |
| `stage` | `attemptEpoch`, `stage` | stageはplanning / retrieving / synthesizing |
| `activity` | `attemptEpoch`, `activity` | nested object、既知type、type固有camelCase属性 |
| `answer.delta` | `attemptEpoch`, `generation`, `text` | 正整数2つ、空でないstring |
| `answer.reset` | `attemptEpoch`, `generation` | 正整数2つ |
| `terminal` | `attemptEpoch`, `status`, optional `errorCode` | statusはcompleted / failed、errorCodeは安全に正規化 |

余剰fieldはraw objectごとreducerへ渡さず、既知fieldだけを新しいobjectへ投影して無視する。これにより
rolling deploymentで安全な追加fieldがあっても既知eventを利用でき、同時に内部fieldがpresentationへ
漏れることを防ぐ。

validation failureは次の2分類にする。

| 分類 | 例 | 処理 |
|---|---|---|
| event-local invalid | unknown event / activity、JSON不正、field型不正 | eventを捨て、state不変、接続継続 |
| protocol integrity failure | missing / invalid Stream ID | eventを捨て、EventSource close、polling-onlyへ移行 |

event-local invalidでdeltaが欠けた場合、一時draftが不完全になることはbest-effort live表示の制約として受容し、
runを失敗させない。terminalまたはpolling後にDBの確定回答へ置き換えて自己修復する。

failed terminalでstatusが正しく、`errorCode`がない、または未知の場合はterminalを捨てない。generic failureへ
正規化し、`回答を生成できませんでした`へ収束させる。completedにerrorCodeがあってもcompletedを受理して
codeを捨てる。status自体が未知または型不正の場合はevent-local invalidとして捨て、pollingのDB statusを待つ。

Redis Stream IDはbackend / BFFと同じcanonical contractで検証する。

```text
<milliseconds>-<sequence>
```

- 2 partはいずれもASCII decimalである。
- `0`以外は先頭zeroを持たない。
- 各partはunsigned 64-bit以下である。
- 全体は41文字以下である。
- sign、空part、追加separatorを許可しない。

比較は文字列辞書順ではなく、2 partを検証後に`BigInt`へ変換して行う。

```text
left < right
  = left.milliseconds < right.milliseconds
    or millisecondsが同じでleft.sequence < right.sequence
```

これにより`9-0 < 10-0`、`1-9 < 1-10`を正しく扱い、JavaScript `number`のsafe integer上限を
超えるuint64でも精度を失わない。

parserはcanonical IDを`BigInt` pairへ変換してtyped eventへ含める。reducerだけが
`lastProcessedEventId`との大小比較を行う。同じ・小さいIDは再処理せず、大きいvalid IDはstale epoch / generationで
表示上無視する場合も処理済みとして前進させる。controllerはparserが返すinvalid IDをpolling-onlyへ劣化させる
だけで、ID比較を行わない。

JSON構造とcitation markerの除去はfrontend parserの責任ではない。Direct / Evidence producerが表示可能textだけを
`answer.delta`へ入れ、frontendはそのtextと既知fieldだけをReactへ渡す。frontend parserへ同じcitation filterを
追加しない。実backend producer、Redis / SSE、BFF、UIを通してJSON構造や完全な`[[N]]`が見えないことは
Operational verificationが所有する。

diagnosticは`unknown_event`、`malformed_data`、`invalid_stream_id`、`unknown_activity`等の固定reasonに限定する。
SSE data、answer text、activity payload、未知discriminator文字列、run / user識別子をconsole、log、metric、
exceptionへ含めない。

### 8. Accessibility and automatic scroll

screen readerへ通知するlive regionと、視覚的に増分更新するdraft本文を分離する。

| UI要素 | ARIA / motion契約 |
|---|---|
| 状態ラベル | `role="status"`または`aria-live="polite"` |
| draft本文 | live regionにしない |
| draft container | 生成中・確定中は`aria-busy="true"` |
| spinner | `aria-hidden="true"`、reduced motionでanimation抑制 |
| terminal通知 | 専用announcerがactiveからの完了遷移だけを1回通知 |

通知文言は次に固定する。

- generation中: `回答を生成中…`
- completed後のDB再取得中: `回答を確定しています…`
- DB確定結果反映時: `回答が完了しました`
- failed: 既存error codeの安全な固定文言
- cancelled: `キャンセルしました`

`ResearchLiveAnnouncer`はthread view内のmessage mapやactive componentの外側に、thread ID単位で安定して残る
Client Componentとして配置する。同じclient sessionでactiveだったrun IDを記録し、Server Component refresh後に
そのrunのDB確定assistant messageが現れた遷移でだけ`回答が完了しました`を1回通知する。completed threadの
初回表示・再訪ではactive観測がないため通知しない。

`ActiveRunStatus`のstage状態ラベルだけをpolite live regionとし、activity本文をその内側へ置かない。draftの各delta、
activity件数の細かな変化、45秒再接続はscreen readerへ逐次通知しない。表示更新でfocusをdraft、status、
thread末尾へ移さない。

scroll containerは更新直前に次を計算する。

```text
distanceFromBottom = scrollHeight - scrollTop - clientHeight
```

`distanceFromBottom <= 96`ならauto-followを維持し、DOM更新後に最下部へ追従する。96pxを超えたらuserが
履歴を読んでいると判断してauto-followを停止する。

auto-follow停止中にdraft、reset後の新本文、確定回答等の新しい表示内容が届いた場合は`最新の回答へ`buttonを
表示する。button押下時に最下部へ移動し、auto-followを再開してbuttonを消す。単なるstage文言の更新だけでは
不要なbuttonを表示しない。

通常はbutton操作によるscrollをsmoothにしてよいが、`prefers-reduced-motion: reduce`では即時移動する。
自動追従はlayout反映後に1回だけ行い、deltaごとに複数のscroll処理を重ねない。

### 9. Test ownership and required cases

#### Runtime parser unit

- 6種類の正常eventを既知fieldだけのtyped eventへ変換する。
- `internal_search.started`、`internal_search.completed`、`external_search.queries_generated`、
  `external_search.candidates_fetched`、`external_search.evidence_selected`、`question.resolved`の6 activity subtypeと
  type固有camelCase fieldを検証する。
- unknown event / activity、JSON不正、object以外、field型不正でstate入力を作らない。
- attemptEpoch / generationの0、負数、小数、`Number.MAX_SAFE_INTEGER`超過を拒否する。
- unknown terminal statusを拒否する。
- failedでerrorCodeなし・未知codeをgeneric failureへ正規化する。
- completedにerrorCodeが付いてもcompletedを受理し、codeを投影しない。
- missing / invalid Stream IDをprotocol integrity failureへ分類する。
- sign、leading zero、空part、追加separator、41文字超過、uint64超過を拒否する。
- canonical IDとuint64上限を正しい`BigInt` pairへ変換する。
- 余剰field、raw data、unknown discriminatorをreducer eventへ残さない。

#### Reducer unit

- 最初の任意公開eventでpositive attemptEpochをpinする。
- activity eventごとに`currentActivity`だけを置き換え、未使用のactivity履歴をstateへ保持しない。
- epoch 1のstage / activity / draftがある状態でepoch 2 markerを受けると、draft / generation / stage /
  activity / attempt-local progressをすべて初期化する。
- epoch 2の最初のeventがstage / activity / delta / terminalの各場合に、epoch 1の表示を残さない。
- 大きいepochをevent適用前に切り替え、小さいepochを表示へ適用しない。
- 同一epochの重複`attempt.started`でdraftを維持する。
- 大きいgeneration resetでdraftを破棄する。
- reset喪失時の大きいgeneration deltaをimplicit resetとして処理する。
- 同一generationの重複resetで現在draftを破棄しない。
- `9-0 < 10-0`、`1-9 < 1-10`、同一ID、uint64上限をpairとして正しく比較する。
- stale epoch / generation eventも大きいvalid IDなら`lastProcessedEventId`を前進させ、表示は変更しない。
- 現在epoch 2で、より新しいStream IDのepoch 1 terminalを受けてもaccepted terminalを返さない。
- replay terminalでaccepted terminalを再度返さない。
- terminal後のdelta / reset / stage / activity / markerでstateが変わらない。

#### Controller lifecycle

- active runごとにEventSourceが最大1 instanceであり、React StrictModeでも同時instanceが1つを超えない。
- active pollingは即時実行し、成功後2秒、失敗後4 / 8 / 10秒を決定的なfake timerで確認する。
- polling requestを重ねず、abort / unmount / run ID変更後に次timerを予約しない。
- 401 / 403 / 404でSSE、draft、timer、request、visibility listenerを回収して`router.refresh()`へ収束し、
  visible復帰や再subscribeでも接続・pollingを再開しない。
- queued開始でpolling queued後に非terminal SSEを受理するとrunningへ進み、draftを表示できる。
- polling terminalを最優先で適用し、active statusは`queued < running`で単調mergeする。
- 正のsafe integer epochだけでSSE / poll共通の単調stage mergeを行い、live / reconnectingでも前進pollを適用する。
- current epochがnullのpoll採用、同一epochの前進 / polling-only activity、小さいepochの無視、大きいepoch resetと
  同response List activity拒否を確認する。epoch欠落・0・不正ではstage / activityをmergeしない。
- poll適用後の遅延SSE stageが表示を巻き戻さず、validな新しいStream IDだけは前進する。
- pollingは新epochでdraft / generationをresetし得るが、draftを復元せずStream IDを進めない。
- terminal後に遅延running responseを受けてもfinalizingから戻らない。
- polling terminalはepochに依存せずfinalizationへ進む。
- `CONNECTING`中は同じEventSource、draft、epoch、generation、Stream IDを維持する。
- `CLOSED`、invalid Stream IDでdraftTextを消去しdraftModeをsuppressedにしてpolling-onlyへ移る。
- stale / replay SSE terminalではEventSourceを閉じずrefreshせず、accepted SSE terminalとpolling terminalが
  競合してもfinalizationを1回だけ実行する。
- completedではdraftを維持して確定中へ進み、failed / cancelledでは即座に非表示にする。
- suppressed draftはpolling completed後にも復活させない。
- finalizationは即時refresh後、2 / 4 / 8 / 10秒で再試行し、cleanup後にtimerを残さない。
- run ID変更、unmountでEventSource、timer、listener、pending requestをcleanupする。
- old runの遅延response / callbackがnew run stateを変更しない。
- hiddenでpolling / finalization retryを休止しEventSourceを維持、visibleで即時確認する。
- thread移動cleanupがcancel APIを呼ばない。

#### Component and scroll

- first delta前は空draftを表示せず、到着後にuser message直後のassistant側へ表示する。
- marker / resetなしのgeneration 2 deltaから安全に一時draftを開始する。
- generation中は`回答を生成中…`、completed再取得中は`回答を確定しています…`を表示する。
- raw JSON、envelope、内部field、sources、missing aspectsをdraftへ表示しない。frontend component testで
  citation marker filterのbackend責任を重複実装しない。
- suffix draftを確定回答として扱わず、terminal後にDB回答へ完全置換する。
- failed / cancelledでdraftを非表示にし、安全な固定文言を表示する。
- `CLOSED`後はdraft本文がDOMから消え、completed検知後にも復活しない。
- draft本文とactivity本文がlive regionでなく、状態ラベルだけがpolite、spinnerがaria-hidden、draftが
  aria-busyである。
- dedicated announcerはactiveからcompletedへの遷移時だけ`回答が完了しました`を1回通知し、completed threadの
  初回表示・再訪、activity、deltaでは通知しない。
- thread Aのactive run観測後にthread Bへ移動し、thread A completedへ再訪しても完了通知を出さない。
- delta / terminal / final replacementでfocusを移動しない。
- 96pxでは追従し、97pxでは位置を維持して`最新の回答へ`を表示する。
- burst deltaでlayout後scrollを重複予約しない。
- `最新の回答へ`押下で追従を再開し、reduced motionではsmooth scrollを使わない。
- 確定回答置換時にdraftとfinal answerを同時表示しない。

#### Frontend integration and downstream ownership

- fake EventSourceからparser、reducer、controller、`LiveAnswerDraft`までの正常delta / retry / terminal flowを
  frontend testで1本以上通す。
- queued polling後にSSE marker / deltaを受理すると`待機中`が消えてdraftが表示され、実polling responseの
  検証済み`recentEvents`が進捗activityへ表示されることをfrontend統合testで通す。
- 既存`ActiveRunStatus`のpolling、visibility、stage / activity表示testを新責任境界へ移行し、保証を削除しない。
- real Redis / backend / BFF / browser / proxyを通す障害・45秒再接続testはOperational verificationへ委譲する。
- real Direct / Evidence producerからUIまでJSON構造と完全なcitation markerが表示されないことはOperational
  verificationが所有する。

## Expected file changes

実装時の責任配置は次を基本とし、同じ責任境界を保つ範囲で命名を調整してよい。

```text
frontend/src/features/research/live/
  events.ts                 # runtime parser、typed event、Stream ID validation / BigInt pair化
  reducer.ts                # pure state transition、Stream ID compare、accepted terminal
  controller.ts             # EventSource / polling lifecycleをtest可能にする境界

frontend/src/features/research/hooks/
  useResearchRunLiveState.ts

frontend/src/features/research/components/
  ActiveRunStatus.tsx       # stage statusと非live-region activityのpresentation
  LiveAnswerDraft.tsx       # draft / finalizing / accessibility
  ResearchLiveAnnouncer.tsx # activeからDB確定表示への1回だけの完了通知
  ResearchThreadView.tsx    # current active runとstable announcerの配置
  ResearchLiveScrollButton.tsx または同等の小さな表示境界
```

対応testは実装fileと同じfeature内へ置く。既存`ActiveRunStatus.test.tsx`は削除して保証を失わせず、分割後の
所有者へcaseを移す。generated type、`components/ui/`、backend schema、BFF route、DB migrationは変更しない。

## Implementation order

1. runtime parserとStream ID validation / pair化unit testを先に追加する。
2. pure reducerとStream ID compare / attempt / generation / reset / replay / accepted terminal testを追加する。
3. fake EventSource / clock / timer / fetchでcontroller lifecycle testを作る。
4. hookをcontrollerへ接続し、既存pollingの保証を移行する。
5. `ActiveRunStatus`をpresentationへ縮小し、`LiveAnswerDraft`を追加する。
6. `ResearchThreadView`へcurrent active runのdraftとscroll制御を配置する。
7. stableな`ResearchLiveAnnouncer`、accessibility、scroll、frontend統合testを追加する。
8. Tests表の追加境界caseを照合し、frontend checkを実行する。

## Decision closure

本sliceの設計未決事項はない。次は本仕様に従った実装と検証へ進む。
