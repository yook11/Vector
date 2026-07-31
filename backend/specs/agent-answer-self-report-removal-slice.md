# 回答生成の自己申告撤去と欠損入力化 slice 仕様

更新日: 2026-07-30

実装状況: Draft

## 位置付け

本sliceは、Evidence Answer Agentの出力を回答本文だけに絞り、精査が申告した不足を出力の自己申告から
agentへの入力へ移す。

現行のEvidence Answer Agentは、本文に加えて「どれを引用したか」「何が足りなかったか」
「どの要望を満たせなかったか」「回答は十分か」を自己申告する。これらは本文の後に生成されるため、
ユーザーから見ると回答が書き終わっているのに確定しない時間になる。同時に、自己申告の内容は
機構が既に持っている事実と重複し、齟齬が出たときは回答のまるごと再生成を引き起こす。

Direct Answer Agentは既に`response_schema=None`のplain text streamで本文だけを返す。
本sliceはEvidence Answer Agentをその形へ揃える。新しい方式の発明ではなく、既存の2つの回答経路の
非対称を解消する。

前提: `research-task-evidence-selection-slice.md`の段1〜段5は実装済み(#71 / #72 / #78)。
`evidence-review-run-scope-slice.md`(Evidence Reviewを Run 単位1回へ)も実装済み。
本sliceは収集と精査の再編が完了した後の、回答生成工程だけを対象とする。

`collection_failures`は`evidence-review-run-scope-slice.md`で既に廃止され、精査の不足は
`EvidenceReviewReport.missing`としてRun単位1本に統一されている。本sliceはその上で、
`missing_aspects`に残っているLLMの自己申告を除去する。

## Work Definition

### Problem

- 回答本文の生成が終わってもユーザーは待たされる。ライブ表示はJSONの`answer`だけを増分
  decodeしており、閉じ引用符の後に`cited_refs` / `missing_aspects` /
  `unfulfilled_requirement_ids`の生成が残る。この時間はユーザーには「回答が書き終わったのに
  確定しない」として現れる。
- `cited_refs`は生成コストを払って必ず捨てられる。`answer`がstrである成功経路では、本文の
  citation markerから無条件に再計算される。
- 回答Agentは精査が申告した不足を知らないまま、採用された根拠だけを見て回答する。
  `EvidenceReviewReport.missing`はagentを通らず、後段で`missing_aspects`へ合成されるだけである。
- 回答の十分性をLLMの自己申告(`sufficiency`)に依存している。自己申告と機構が観測した事実の
  齟齬がValidationErrorになり、回答をまるごと再生成する。`answered`かつ`missing_aspects`非空は
  contractで必ず失敗するが、その排他はpromptに書かれていない。
- `unfulfilled_requirement_ids`は要望充足の自己申告であり、`missing_aspects`の自由文と情報が
  重複する。
- token上限で打ち切られた応答を検出できているのは、JSONが閉じないという副作用によるものである。
  出力をplain textへ移すとこの検出が失われ、切れた本文を正常な回答として確定してしまう。
  `finish_reason`の`MAX_TOKENS`は現状`STOP`と同じ正常終了として扱われている。
- 回答本文の`max_output_tokens`は2048であり、JSONの枠と3つのarrayと分け合っている。
  Evidence Reviewerは同じ理由(上限まで出力すると切れる)で16384へ引き上げられているが、
  回答本文は据え置きのままである。

### Evidence

#### 出力契約

- `IncrementalJsonAnswerExtractor`(288行)はroot直下のstring型`answer`だけをdecodeする。
  `EVIDENCE_ANSWER_GEMINI_SCHEMA`のproperty順は
  `sufficiency` / `answer` / `cited_refs` / `missing_aspects` / `unfulfilled_requirement_ids`で、
  `propertyOrdering`は指定していない。
- `EvidenceAnswerDraft.answer`は`NonBlankText = Annotated[str, StringConstraints(...)]`であり、
  strでないdraftは必ずValidationErrorになる。したがってdraftが成立する経路では常に
  `isinstance(raw.answer, str)`が真であり、`finalize_evidence_answer_draft()`が
  `cited_refs = marker_refs`で無条件に上書きする。LLMの`cited_refs`が結果へ残る経路は存在しない。
- 上書きを記録する`cited_refs_recomputed_from_markers`を含むdefect列は、`flow.py`で
  `draft, _defects = finalize_evidence_answer_draft(...)`として破棄される。log、metric、spanの
  いずれにも出ていない。
- `EvidenceAnswerDraft`のvalidatorは`sufficiency == "answered"`のとき`missing_aspects`が非空なら
  ValidationErrorを送出する。`classify_answer_synthesis_failure()`はValidationErrorを
  `RETRY_IN_REQUEST`へ分類し、`_MAX_ATTEMPTS = 2`のループがstreamの最初から再生成する。
  差分修復ではない。
- `EVIDENCE_ANSWER_INSTRUCTIONS`には`sufficiency`と`missing_aspects`の使い分けも排他制約も
  書かれていない。schemaのdescriptionが「insufficientのとき必須」と述べるだけである。
- `EVIDENCE_ANSWER_AGENT`は`max_output_tokens=2048` / `temperature=0.2`。
  `EVIDENCE_REVIEWER_AGENT`は`max_output_tokens=16384`。
- `DIRECT_ANSWER_AGENT`は`output_type=DirectAnswerDraft`(`answer`のみ) / `response_schema=None`で
  宣言され、`GeminiAgentRuntime.invoke_stream()`は`agent.response_schema is not None`で
  structured要求を切り替える。`DirectAnswerFlow._generate_draft()`はfragmentをそのまま
  live draftへ流し、連結後に`self._agent.output_type(answer=answer)`でdraftを構築する。
- `AnswerVisibleTextFilter`は表示から`[[N]]`を除く。Direct Answerは表示に加えて本文からも
  markerを除去するが、Evidence Answerは本文にmarkerを保持し、frontendが引用linkへ変換する。

#### 収集・精査の現状

- `EvidenceCollectionOutcome`は`internal_evidence` / `internal_deduplicated_count` /
  `external_search` / `task_reports`(min_length=1) / `review`を持つ。
- `ResearchTaskReport`はtask単位の収集系だけを持つ。`task_index` / `research_goal` /
  `internal_collection`(2値) / `external_collection`(4値) / `time_filter_failure_reason` /
  `generated_queries` / `provider_failed_query_count` / `internal_candidate_count` /
  `external_candidate_count`。精査結果はここに無い。
- `EvidenceReviewReport`はRun単位の精査系を持つ。`review`(`succeeded` / `failed` /
  `skipped_empty`) / `review_failure_reason` / `internal_evidence_count` /
  `external_evidence_count` / `dropped_selection_count` / `missing`。
- `missing`は`EVIDENCE_REVIEW_MISSING_LIMIT`(8)件、1件`MISSING_ITEM_MAX_CHARS`(200)字でclamp
  される。`review == "skipped_empty"`のときvalidatorが`missing`を空に強制する。
  `review == "failed"`は根拠ゼロと`review_failure_reason`を要求する。
- `collection_failures`は廃止済み。`AnswerPlanSummary`は`plan_type`だけを持ち、
  `AnswerQuestionResult`のvalidatorからも該当分岐が消えている。
- `_missing_aspects()`の構成要素は、`_RETRIEVAL_EMPTY_MISSING`(evidence空) /
  `_INCOMPLETE_TASK_MISSING`(`review == "failed"`または候補が内外ともゼロのtask) /
  `_EXTERNAL_TASK_STATUS_MISSING`(time filterのみtask単位) / `outcome.review.missing` /
  `draft_missing_aspects`(LLM由来) / `requirement_missing_aspects`(自己申告由来)。
- `_derive_evidence_status()`は`sources`と`missing_aspects`だけから導出する。`outcome`を見ない。
- `AnswerQuestionResult`のvalidatorは`status == "answered"`のとき
  `plan_type == "search"`かつ`sources`が空ならValueErrorを送出する。
- `EvidenceAnswerInput`は`request` / `evidence` / `target_time_window` / `previous_error`のみを
  持つ。唯一の欠損伝達が`_NO_EVIDENCE_BLOCK`で、evidence 0件のときだけpromptで伝えている。

#### finish reason の分類

- `GeminiAgentRuntime._stream_fragments()`は`_BLOCKED_FINISH_REASONS = {"SAFETY", "RECITATION"}`
  に該当する`finish_reason`を`AIProviderOutputBlockedError`へ写す。それ以外の
  `finish_reason`が1つでも観測されれば`terminal_reason_seen = True`となり`normal_eof`扱いになる。
  `MAX_TOKENS`は`STOP`と区別されない。
- `_FINISH_REASON_TO_CONTENT_REASON`は`SAFETY` / `RECITATION` / `BLOCKLIST` /
  `PROHIBITED_CONTENT` / `SPII`の5つを写像に持つが、`_BLOCKED_FINISH_REASONS`は前2つだけである。
  後3つが返った場合はblocked扱いされず正常終了になる。
- `finish_reason`が一度も観測されなかった場合は
  `AIProviderNetworkError(reason=GeminiStateReason.STREAM_TRUNCATED)`になる。
- `AIProviderFailureMode.ATTEMPT_SCOPED`は「その実行だけの問題か」を表す回復クラスである。
- `classify_answer_synthesis_failure()`は`AIProviderStateError | AIProviderContentError`を
  `DO_NOT_RETRY_IN_REQUEST`へ分類する。`classify_direct_answer_failure()`も同じである。
- `runtime/gemini.py`は`#36`以降変更されていない。

#### missing_aspects の consumer

- `AgentMessage.missing_aspects`はJSONB arrayとしてDB制約を持ち、`_history_for_prompt()`が
  履歴から集約してQuestion Context Agentへ渡す。
- Question Context Agentのinstructionsは「各assistant messageのmissing_aspectsは、その回答で
  満たせなかった保存済みの要望である。今回も扱うべきものだけを対応するrequirementへ昇格する」と
  規定している。要望由来の表現であり、現在の構成要素(収集・精査の不足)と一致していない。
- `previous_answer_had_missing_aspects` metricが`_latest_assistant_has_missing_aspects()`から
  記録される。
- `AnswerQuestionResult.status`はDBにもAPI responseにも出ていない。読んでいるのは同classの
  validatorのみである。
- `record_answer_synthesis_outcome()`の`status`ラベルは`EvidenceAnswerDraft.sufficiency`から
  渡されている。

### Invariants

#### 打ち切られた応答を正常終了にしない

- `finish_reason`を次のとおり区別する。plain textへ移す前にこの区別を用意する。JSONが閉じない
  ことによる偶然の検出に依存した状態で出力形式を変えない。

  | finish_reason | 扱い |
  |---|---|
  | `STOP` | 正常終了 |
  | `MAX_TOKENS` | 未完成。分類済みerrorとして送出する |
  | `SAFETY` / `RECITATION` / `BLOCKLIST` / `PROHIBITED_CONTENT` / `SPII` | 出力ブロック |
  | 観測されない | 通信切断(現行の`STREAM_TRUNCATED`) |

- `MAX_TOKENS`は`AIProviderStateError`系の専用leafで表す。回復クラスは`ATTEMPT_SCOPED`とする。
  同じ入力でも書き方次第で収まるため、その実行だけの問題である。reasonはadapter local検知の
  並びへ追加する。
- 打ち切りはrequest内でretryする。`classify_answer_synthesis_failure()`と
  `classify_direct_answer_failure()`で、この型だけを`RETRY_IN_REQUEST`とする。他の
  `AIProviderStateError`の分類は変えない。
- retryのrepair contextへ「前回は長さ上限で打ち切られた」を伝える。伝えなければ同じ長さで
  再び切れ、2 attemptを消費するだけになる。
- blocked-setを`_FINISH_REASON_TO_CONTENT_REASON`のkeyと一致させる。写像を持つ理由が
  「blocked-setで先に絞る前提」であり、両者が食い違っている状態は写像側の契約違反である。
- 既にyield済みのfragmentがある状態で分類済みerrorを送出する。live draftはsessionのabortで
  破棄され、frontendはretryのresetを待つ。現行のJSON不正時と同じ挙動であり、SSE契約は変わらない。

#### 回答Agentの出力は本文だけである

- Evidence Answer Agentの出力は回答本文のみとする。引用、不足、要望充足、十分性を出力させない。
- structured outputを使わない。`response_schema=None`でplain text streamを受け取る。
  Direct Answer Agentと同じ形にする。
- streamの終了と回答の完成を一致させる。本文が書き終わった後にユーザーを待たせる生成を残さない。
- `max_output_tokens`を2048から8192へ引き上げる。本文専用になっても2048は日本語Markdownの
  調査回答に対して窮屈であり、Evidence Reviewerを16384へ上げたのと同じ理由が本文にも当てはまる。
  日本語のtoken比は公表値がないため保守側に1.0 token/字を仮定すると、上限到達時は概算8,000字で
  ある。見出し・箇条書き・表とcitation markerを含む調査回答に対して余裕があり、打ち切りを
  例外的な事象へ寄せられる。

#### 引用は本文のmarkerが正本である

- `cited_refs`は本文の`[[N]]`から決定的に算出する。現行の`_citation_refs_from_answer()`の規則
  (初出順、重複排除)を維持する。
- evidenceに存在しないrefを本文が参照した場合はdraft不正として扱う。現行の
  `_validate_draft_citations()`の判定を維持する。
- markerの要求はevidenceの有無で分ける。

  | evidence | marker | 扱い |
  |---|---|---|
  | 非空 | 1件以上 | 正常 |
  | 非空 | 0件 | draft不正。retryし、2回目も不正ならunavailable |
  | 空 | 0件 | 正常。`sources`は空になる |
  | 空 | 1件以上 | 不正(evidenceに無いrefとして既存の検証が弾く) |

  現行はこの分岐を`sufficiency == "answered"`で行っているが、自己申告への依存である。
  evidenceが1件でもあるなら引用のない回答は接地していないため不正とし、evidenceが0件のRunでは
  markerが無いことが正しい。
- 表示から`[[N]]`を除く`AnswerVisibleTextFilter`は変更しない。Evidence Answerは本文にmarkerを
  保持し続ける。

#### 回答を作れなかったことを回答の一種として表さない

- 生成が尽きた場合の結果を、回答draftと別の型で表す。現行のfallbackは
  `sufficiency="insufficient"`と`missing_aspects=[...]`を自己申告することでvalidatorを通して
  いるため、自己申告を撤去すると成立しない。evidenceがあるRunでfallbackすると、markerの無い
  本文から`sources`が空になり、`missing_aspects`も機構由来だけでは空になり、
  `AnswerQuestionResult`のvalidatorが「answeredなのにsourceが無い」で落ちる。
- 生成不能の結果は`failure_code`だけを持つ。ユーザーへ見せる定型本文と`missing_aspects`の1行は
  `result_assembly`が所有する。回答生成が失敗したときの表示を決めるのは結果を組み立てる側で
  あり、生成工程ではない。
- 定型本文と`missing_aspects`の文言は現行の値を維持する。

#### 精査の不足はagentへの入力として渡す

- 回答Agentへ`EvidenceReviewReport.missing`を渡す。何ができなかったかを伝えた上で、採用された
  根拠を渡す。
- それ以外を渡さない。`research_goal`ごとの達成状況、`ResearchTaskReport`の収集診断、
  `review`の状態値、`review_failure_reason`はagentの入力に含めない。根拠が無い理由が
  「見つからなかった」か「採用されなかった」かは回答の書き方を変えない。運用者向けの情報を
  model-visibleにしない。
- `missing`はRun単位で1本であり、調査目的との対応を持たない。対応づけを復元して渡さない。
- `missing`は`sanitize_for_untrusted_block()`と`<untrusted_input>`境界を通す。reviewerの
  生成物であり、信頼済みテキストとして展開しない。
- `missing`は既に8件×200字でclampされているため、prompt側で追加のcapを設けない。
- evidence 0件のときに`_NO_EVIDENCE_BLOCK`で伝えている内容は維持し、`cited_refs`と
  `sufficiency`への指示だけを撤去する。`missing`の受け渡しとは独立であり、両方が成り立つRunが
  ある(候補はあったが1件も採用されず、不足だけが申告された場合)。

#### 十分性をLLMに評価させない

- `sufficiency`を撤去する。回答が十分かどうかの判断をLLMの自己申告から取らない。
- `unfulfilled_requirement_ids`を撤去する。要望充足の自己申告は`missing_aspects`の自由文と
  情報が重複する。requirement単位で追跡する機構は設けない。
- `record_answer_synthesis_outcome()`の`status`ラベルを廃止する。出所が消えるためであり、
  `result` / `retry_used` / `fallback_used` / `failure_code`は維持する。

#### missing_aspectsは機構と精査の由来だけで組み立てる

- `AnswerQuestionResult.missing_aspects`と`AgentMessage.missing_aspects`を維持する。Question
  Context Agentへの会話文脈と`previous_answer_had_missing_aspects` metricという2つのconsumerが
  存在する。
- 構成要素からLLMの自己申告を除く。残すのは次である。
  - `_RETRIEVAL_EMPTY_MISSING`(evidence空)
  - `_INCOMPLETE_TASK_MISSING`(精査失敗または候補が内外ともゼロのtask)
  - `_EXTERNAL_TASK_STATUS_MISSING`(time filter適用失敗)
  - `outcome.review.missing`
  - 回答生成が不能だった場合の1行
- `review.missing`は回答Agentの入力と`missing_aspects`の両方に載る。用途が別(回答生成への通知 /
  次ターンへの引き継ぎ)であり、重複ではない。
- 要望由来の項目(`回答要望を満たせませんでした: ...`)は消える。
- Question Context Agentのinstructionsを追随させる。`missing_aspects`は「満たせなかった要望」
  ではなく「前回の回答で確認できなかったこと・完了しなかった調査」になる。現行の説明と昇格の
  指示が実態と食い違うため、prompt versionを上げて記述を合わせる。
- `status`の導出規則(`missing_aspects`が非空なら`insufficient`、`sources`が空なら
  `insufficient`)は変更しない。`status`はDBにもAPIにも出ておらずconsumerが存在しない。
  意味論の整理は別スコープで扱う。

#### ユーザーに不足の一覧を見せない

- frontendの「確認できなかった点」の表示を撤去する。回答本文が不足を踏まえて書かれるため、
  同じ情報を別枠で列挙しない。
- API responseの`missingAspects`は互換のため維持する。撤去は後続タスクで行う。

### Non-goals

- `status` / `insufficient`の再設計と、「どのtaskのどの段で失敗したか」のLogfireへの構造化。
  `project_error_visibility_logfire`の計画に乗せる別スコープとする。
- API responseから`missingAspects`を削除すること。
- requirement単位の充足を追跡するmetricを新設すること。
- Direct Answer Agentのprompt、出力契約、live delivery機構を変更すること。打ち切り検出に伴う
  failure分類の追随だけを行う。
- 収集と精査の再編(`Researcher` / `EvidenceReviewer` / `ResearchTaskReport` /
  `EvidenceReviewReport`)に手を入れること。本sliceは`review.missing`を読むだけである。
- `AnswerSource`、`AnswerPlanSummary`、`EvidenceCollectionOutcome`のshapeを変更すること。
- retryのattempt数(2)、`AnswerVisibleTextFilter`、live delta transport(coalesce間隔、
  Redis Stream、SSE)を変更すること。
- `missing_aspects`の履歴集約規則(件数・文字数cap)を変更すること。

### Done

- `finish_reason`の`MAX_TOKENS`が未完成として分類され、request内でretryされる。blocked-setが
  写像のkeyと一致している。
- Evidence Answer Agentが`response_schema=None`で宣言され、出力が回答本文だけになっている。
- streamの終了時点で回答が完成しており、本文の後に待つ生成が存在しない。
- `IncrementalJsonAnswerExtractor`と`parse_evidence_answer_final_json()`が削除されている。
- 回答Agentのpromptが`EvidenceReviewReport.missing`を`<untrusted_input>`境界付きで受け取っている。
- 収集診断と精査の状態値がagentの入力に現れない。
- 生成不能の結果が回答draftと別の型で表され、evidenceがあるRunでも`AnswerQuestionResult`が
  構築できる。
- `missing_aspects`が機構と精査の由来だけで組み立てられ、DB永続化とQuestion Contextへの
  引き継ぎが働く。Question Context Agentのinstructionsが新しい意味に追随している。
- frontendに「確認できなかった点」の表示が存在せず、API responseの`missingAspects`は残っている。
- 既存のregression(回答shape、citation検証とlink描画、progress stage、live delta、
  resource lifecycle、本文非露出)がすべて通る。

## 責任境界

| 責任 | Runtime | AnsweringRunner | EvidenceAnswerFlow | Agent | result_assembly | frontend |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| finish reasonの分類 | ○ | - | - | - | - | - |
| 打ち切りのretry判定 | - | - | ○ | - | - | - |
| review.missingの受け渡し | - | ○ | - | - | - | - |
| 欠損のprompt rendering | - | - | - | prompt | - | - |
| 本文の生成 | 実行 | - | 起動 | 宣言 | - | - |
| markerからのcited_refs算出 | - | - | ○ | - | - | - |
| citation整合の検証 | - | ○ | ○ | - | - | - |
| 生成不能時の表示文言 | - | - | - | - | ○ | - |
| missing_aspectsの組み立て | - | - | - | - | ○ | - |
| 本文のmarker→link変換 | - | - | - | - | - | ○ |

## 変更後のデータの流れ

| 情報 | 出所 | 回答Agentの入力 | missing_aspects | UI |
|---|---|:---:|:---:|:---:|
| 精査が申告した不足 | `EvidenceReviewReport.missing` | ○ | ○ | ✕ |
| evidence 0件 | evidence空 | ○ | ○ | ✕ |
| 精査失敗・候補ゼロのtask | `review` / `task_reports` | ✕ | ○(固定文言) | ✕ |
| 期間フィルタの適用失敗 | `external_collection` | ✕ | ○ | ✕ |
| 回答生成が不能 | flowの結果型 | - | ○ | ✕ |
| 収集診断・精査の状態値 | `ResearchTaskReport` / `review` | ✕ | ✕ | ✕ |
| 満たせなかった要望 | `unfulfilled_requirement_ids` | 撤去 | 撤去 | ✕ |
| 引用したref | 本文のmarker | 撤去 | - | ○(link) |
| 回答の十分性 | `sufficiency` | 撤去 | - | ✕ |

## 別タスクへ送る判断

- `status` / `insufficient`の再設計と失敗の構造化観測。
- API responseからの`missingAspects`削除。
- 打ち切りが頻発する場合の`max_output_tokens`の再調整。本文専用にした後の実測で判断する。
- 分類表に列挙していない`finish_reason`(`OTHER` / `LANGUAGE` / `MALFORMED_FUNCTION_CALL` /
  `UNEXPECTED_TOOL_CALL`)の扱い。現状はstream・非streamのいずれも正常終了になる。打ち切りではなく
  本sliceのProblemの外にあり、扱うには新しいerror leafと回復クラスの語彙を決める必要がある。

## Test contract

### finish reason の分類

- `finish_reason`が`STOP`のstreamが正常終了する。
- `finish_reason`が`MAX_TOKENS`のstreamが分類済みerrorを送出し、正常終了しない。
- `MAX_TOKENS`のerrorがrequest内でretryされ、repair contextに打ち切りが伝わる。
- 2回目も`MAX_TOKENS`で打ち切られたEvidence Answerがunavailableへ落ちる。
- 2回目も`MAX_TOKENS`で打ち切られたDirect Answerが分類済みerrorを送出する。
- `BLOCKLIST` / `PROHIBITED_CONTENT` / `SPII`が出力ブロックとして扱われる。
- `finish_reason`が観測されないstreamが現行どおり通信切断として扱われる。
- 打ち切り時にfragmentが既にyieldされていても、live draftがabortされresetが発火する。

### 出力契約

- Evidence Answer Agentの宣言が`response_schema=None`であり、structured outputを要求しない。
- streamのfragmentが加工されずにlive draftへ流れ、連結結果が回答本文になる。
- JSONを模した本文(`{"answer": ...}`)がそのまま本文として扱われ、field抽出が起きない。
- 空白のみの本文がdraft不正として扱われる。

### citation

- 本文の`[[1]][[2]]`から`cited_refs`が初出順・重複排除で算出される。
- evidenceに存在しないrefを本文が参照したdraftが不正として扱われる。
- evidenceが非空でmarkerが無い本文が不正として扱われ、retryされる。
- evidenceが空でmarkerが無い本文が正常に成立し、`sources`が空になる。
- `sources`が`cited_refs`に対応するevidenceだけで構成される。

### 欠損の入力

- `EvidenceReviewReport.missing`が回答Agentの入力に含まれる。
- `missing`が空のRunで欠損ブロックが入力に現れない。
- `research_goal`、`ResearchTaskReport`の収集診断、`review`の状態値、
  `review_failure_reason`が入力に現れない。
- `missing`が`sanitize_for_untrusted_block()`を通り、`<untrusted_input>`境界の内側に置かれる。
- evidenceが0件のRunで、evidence不在が伝わり、`cited_refs`と`sufficiency`への指示を含まない。
- evidenceが0件かつ`missing`が非空のRunで、両方が入力に現れる。

### 生成不能

- evidenceがあるRunで生成が尽きたとき、`AnswerQuestionResult`が構築でき、`status`が
  `insufficient`になる。
- 生成不能の結果に定型本文と`missing_aspects`の1行が付く。
- 生成不能の`failure_code`がmetricへ記録される。

### missing_aspects

- `missing_aspects`にLLM由来の自由文が含まれない。
- `review.missing`が`missing_aspects`にも残る。
- 精査失敗・候補ゼロのtask・time filter失敗の由来の文言が現行どおり入る。
- 要望由来の文言(`回答要望を満たせませんでした: ...`)が現れない。
- `missing_aspects`がDBへ永続化され、次ターンのQuestion Context Agentの入力へ集約される。
- `missing_aspects`が空のRunで`status`が`answered`、非空のRunで`insufficient`になる。

### metric

- `record_answer_synthesis_outcome()`に`status`ラベルが存在しない。
- `result` / `retry_used` / `fallback_used` / `failure_code`が現行どおり記録される。

### frontend

- 確定回答に「確認できなかった点」の領域が描画されない。
- `missingAspects`が非空のAPI responseでも表示が増えない。
- 本文のmarkerが引用linkへ変換される既存の挙動が変わらない。

### Architecture boundary

- `json_answer_extractor.py`と`final_json.py`がリポジトリに存在しない。
- `evidence_answer/`が`RawEvidenceAnswerDraft`を持たない。
- 回答Agentのpromptが`ResearchTaskReport`と`EvidenceReviewReport`の状態値をimportしない。

## 実装順

4段に分ける。段0で打ち切りの検出を用意し、段1で待ち時間が消え、段2で回答の内容が変わり、
段3で表示が変わる。

段0を先に入れる理由は、現行の打ち切り検出がJSONが閉じないという副作用に依存しており、段1で
出力形式を変えると検出が失われるためである。順序を逆にすると、段1のmergeから段0のmergeまでの
間だけ、切れた本文を正常な回答として確定する退行が生きる。

1. **段0 打ち切りの分類**: `finish_reason`の`MAX_TOKENS`を分類済みerrorとして送出し、
   blocked-setを写像のkeyと一致させる。両flowのfailure分類でこの型を`RETRY_IN_REQUEST`とし、
   repair contextへ打ち切りを伝える。振る舞いの変更はここに閉じ、出力契約は変えない。
2. **段1 出力の縮小**: `sufficiency` / `cited_refs` / `missing_aspects` /
   `unfulfilled_requirement_ids`をLLM出力から撤去し、`response_schema=None`のplain text stream
   へ移す。`IncrementalJsonAnswerExtractor`、`final_json.py`、`RawEvidenceAnswerDraft`を削除し、
   `finalize_evidence_answer_draft()`をmarker算出とcitation検証だけに縮小する。生成不能の結果を
   別の型へ分離する。`result_assembly.py`から`draft_missing_aspects`と
   `requirement_missing_aspects`の経路を外し、生成不能の文言を追加する。metricの`status`ラベルを
   廃止する。promptから出力fieldへの言及を撤去する。`max_output_tokens`を8192へ引き上げる。
   この段でstream終了と回答完成が一致する。
3. **段2 欠損の入力化**: `EvidenceAnswerInput`へ`review.missing`を追加し、`AnsweringRunner`が
   渡す。promptへ欠損ブロックを追加する。Question Context Agentのinstructionsを新しい
   `missing_aspects`の意味へ追随させる。
4. **段3 表示の撤去**: `ResearchAnswerSlot`から「確認できなかった点」を削除する。APIは
   変更しない。

## 影響範囲

- `app/analysis/ai_provider_errors.py` / `app/analysis/gemini_error_translator.py` — 打ち切りの
  error leafとreason追加
- `app/agent/runtime/gemini.py` — finish reasonの分類、blocked-setの整合
- `app/agent/answering/failure.py` — 打ち切りのretry分類(両flow)
- `app/agent/answering/evidence_answer/` — agent宣言、contract、flow、validation、prompts。
  `json_answer_extractor.py`と`final_json.py`は削除
- `app/agent/answering/result_assembly.py` — `missing_aspects`の組み立て、生成不能の文言
- `app/agent/answering/metrics.py` — `status`ラベルの廃止
- `app/agent/running/answering_runner.py` — `review.missing`の受け渡し
- `app/agent/question_context/prompts.py` — instructionsの追随、prompt version
- `frontend/src/features/research/components/ResearchAnswerSlot.tsx` — 「確認できなかった点」の削除

DB schema変更なし。API response shape変更なし(`missingAspects`は維持)。新規dependencyなし。
`/gen-types`の再生成は不要。

### 実装後に確認する運用値

- 回答本文が書き終わってから確定表示までの時間。段1の効果はここに出る。
- `agent_provider_call` spanの`attempt_number`と`gen_ai.usage.output_tokens`。自己申告由来の
  ValidationErrorが消えることで再生成の頻度が下がるはずであり、残る再生成の原因を切り分ける。
- `vector.agent.answer_synthesis.outcome`の`failure_code`。
  `answer_synthesis_pydantic_validation_failed`と
  `evidence_answer_response_gemini_not_json`が消え、打ち切り由来のcodeが現れることを確認する。
- 打ち切りの発生率。段0で可視化され、段1の`max_output_tokens`引き上げで下がるはずである。
  下がらない場合は値を再調整する。
- 欠損を入力に渡した後の回答の書き方。段2の効果として、確認できなかったことに言及する回答が
  現れるかを確認する。
