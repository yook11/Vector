# 工程ごとの時間上限と回答生成失敗

## Problem

工程の待機時間が無制限、または再試行のたびに更新されるため、工程全体の処理時間を制限できない。
また、根拠付き回答の生成不能を固定文として保存する経路があり、生成失敗でもrunがcompletedになる。

## Evidence

- planning / internal search / direct answer / evidence answerに工程全体の上限がなかった。
- external queryは30秒、evidence reviewは各試行30秒で最大2試行だった。
- `agent-answer-self-report-removal-slice.md`の生成不能を固定文へ変換する契約が、根拠付き回答の保存に引き継がれていた。
- 回答保存は1トランザクションでrunとthreadをロックし、明示的なロック待ち制限がなかった。

## Invariants

| 工程 | 上限と範囲 |
| --- | --- |
| planning | runtime準備と再試行を含め15秒 |
| internal search | 検索1回に渡された全クエリのキャッシュ・埋め込み・DB検索を含め15秒 |
| external query | 1回10秒 |
| external provider | 既存のクエリごと15秒を維持 |
| AgentCore HTTP | 既存の通信待機10秒を維持 |
| evidence review | runtime準備と再試行を含め15秒 |
| direct / evidence answer | runtime準備、再試行、ストリーミングを含め15秒 |
| answer save | 保存トランザクション内の各ロック取得待ち3秒 |

- direct / evidence answerの15秒は回答工程共通の時間定義を参照する。
- 回答生成開始済みrunの回収猶予は30秒とし、回収期限は回答開始時刻＋工程制限15秒＋猶予30秒とする。
- 再試行や回答断片の受信で時間枠を更新しない。既存の再試行対象と最大回数を維持する。
- 時間超過時に中断を開始し、既存のストリーム・clientの終了処理を行う。終了処理の所要時間は上限秒数を超える場合がある。
- 外部キャンセルとrun継続停止は工程の時間超過に変換しない。下位処理由来のTimeoutErrorを工程の時間枠超過と取り違えない。
- planningの時間超過はPlanningError、internal searchはInternalSearchError(TIMEOUT)、reviewはEvidenceRunFailedとなる。内部検索・外部検索・レビューの失敗後に回答へ進む既存契約は維持する。
- 直接回答と根拠付き回答の最終的な生成失敗は、それぞれDirectAnswerError / EvidenceAnswerErrorとして通知する。workerはgeneration_unavailableでrunをfailedにする。
- 根拠付き回答の成功戻り値はEvidenceAnswerDraftのみ。EvidenceAnswerUnavailableと生成不能の固定文への変換を廃止する。
- 生成に失敗した下書きはabortし、失敗後のfallback generationやfinishは送らない。回答・引用元・handoffの完了保存は行わない。
- 回答自体を生成できた場合のanswered / insufficient判定は維持する。生成不能はinsufficientへ変換しない。
- 工程の時間超過だけではrunをdeadline_exceededにしない。既存の終端状態とattempt epochの条件は維持する。
- 失敗記録の試行数は実際の呼び出し回数とし、runtime準備中の失敗では0を許容する。成功記録は1以上とする。
- 保存時はバインド値によるset_configでlock_timeoutをトランザクション限定に設定する。ロック待ち超過では全体をrollbackし、既存の保存失敗処理へ渡す。保存retryは追加しない。
- lock_timeoutは保存全体や失敗記録を含む総時間の上限ではない。

## Non-goals

run全体の60秒期限、継続確認、定期回収、Taskiqの180秒、handoff整理の時間枠は変更しない。
60秒超過後の回答採用は別の作業とする。APIレスポンス形状、DBスキーマ、依存は変更しない。

## Done

- 待機・再試行・runtime準備・連続fragmentで上限とキャンセルを検証する。
- 両回答経路の最終的な生成失敗をfailedとして保存し、回答と引用元を保存しないことを検証する。
- 実Postgresでrun/threadのロック競合、全体rollback、commit/rollback後の設定解除を検証する。
- backendのlint・format check・unit tests・make test-integrationを通す。

この仕様の生成失敗契約は、過去のsliceにあるEvidenceAnswerUnavailable・固定文保存の契約を置き換える。
