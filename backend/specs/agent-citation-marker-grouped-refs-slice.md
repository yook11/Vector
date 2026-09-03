# 引用 marker のグループ形受理 slice 仕様

> 更新: 生成不能時の `EvidenceAnswerUnavailable` / 固定文保存は廃止し、[工程の時間上限と生成失敗の契約](agent-stage-timeouts-slice.md)へ置き換える。

## 位置付け

`question-answering-inline-citation-slice.md` (Slice C-3) で確定した citation marker
構文の**受理範囲を拡張する** slice。正準形 `[[N]]` は変更しない。backend の受理側
4 機構と frontend の描画 1 機構を同時に広げる。API response shape / DB schema の
変更は無い。

## Problem

evidence answer で複数の出典を引くとき、モデルは `[[1], [5]]` の形を自然に出力する。
現行の受理側は `[[数字]]` 単体しか認識しないため、この形が出ると以下が起きる。

- **本文の他所に正しい marker があるとき**: グループ形は検証も除去もされず、
  `citation_integrity` にも marker として認識されないため warning すら鳴らず、
  生テキストのまま画面に出る (サイレント劣化)。
- **本文の marker が全てグループ形のとき**: 「evidence があるのに marker 0 件」で
  `EvidenceAnswerDraftInvalidError` → in-request retry 1 回 → 継続失敗で
  `EvidenceAnswerUnavailable` に落ちる。

現行の対処はプロンプトの禁止行 1 行 (`[[1], [2]]` の形式は使わない。) だけで、
これは実際には守られていない。指示で抑え込むのをやめ、境界で受け止める。

## Evidence (調査済みの前提事実)

- **正準形の定義箇所は 3 層で一致している**:
  - プロンプト: `backend/app/agent/answering/evidence_answer/prompts.py:37` /
    `:39-40` (version は同 `:13` の `v6`)
  - backend パース: `evidence_answer/validation.py:15` と
    `agent/runs/citation_integrity.py:9` が同一の `r"\[\[([0-9]+)\]\]"` を
    **それぞれ独立に定義**、`direct_answer/flow.py:44` が除去用の
    `r"\[\[[0-9]+\]\]"`、`answering/visible_text.py:10-122` が手書き状態機械
  - frontend: `features/research/markdown/remark-citation-markers.ts:29` の
    `/\[\[(\d+)\]\]/g`
- **`app/agent/answering/` と `app/agent/runs/` の間に import は 1 本も無い**
  (双方向とも 0 件)。現在の重複はモジュール境界を保つ結果であり、意図の共有では
  ない。
- **グループ形の現行挙動は実測済み** (python3 / node で実行):
  - `[[1], [5]]` → backend refs `[]` / frontend バッジ 0 個・生テキスト表示
  - `[[1]][[5]]` → backend refs `['1','5']` / バッジ 2 個
  - `[[1]], [[5]]` → backend refs `['1','5']` / バッジ 2 個の間に `, ` が残る
- **`mdast-util-find-and-replace` の `ReplaceFunction` は配列返却を正式サポート**:
  `Array<PhrasingContent> | PhrasingContent | string | false | null | undefined`。
  `null` / `undefined` / `''` / `[]` は match の除去、`false` は非置換
  (`node_modules/mdast-util-find-and-replace/readme.md:233-238`、実装は
  `lib/index.js:158-179`)。1 マッチから複数バッジを返せる。
- **`_sources_for_citations` (`result_assembly.py:79-85`) は cited_refs 集合のみに
  依存する**。パーサが複数 ref を返せば sources は自動で追随し、変更不要。
- **禁止行を substring 固定しているテストが 1 件だけある**:
  `backend/tests/agent/answering/evidence_answer/ai/test_prompt_schema.py:414`。

## 合意済みの設計判断

### 1. 正準形は変えず、受理範囲だけを広げる

`[[1]][[2]]` を引き続き正準形とし、プロンプトの「複数の出典を引く場合は
`[[1]][[2]]` のように連続して書く。」は**残す**。バッジ間の視覚的区切りは
`SourcePreviewBadge.tsx:150` の `mx-0.5` だけなので、連続形の方が表示が揃う。
禁止行「`[[1], [2]]` の形式は使わない。」だけを削除する。

受理を広げる理由は表示品質ではなく、モデル出力の揺れをサイレント劣化に変えない
ことにある。

### 2. 受理する文法

```
citation_group := "[[" ref ( separator ref )* "]]"
ref            := [0-9]+
separator      := "]" "," hspace* "["
hspace         := " " | "\t"
```

Python:
```python
r"\[\[([0-9]+(?:\],[ \t]*\[[0-9]+)*)\]\]"
```

JavaScript:
```js
/\[\[(\d+(?:\],[ \t]*\[\d+)*)\]\]/g
```

group(1) から `[0-9]+` を全て取り出し、初出順・重複排除で ref 列にする。

受理例と非受理例:

| 入力 | refs | 備考 |
|---|---|---|
| `[[1]]` | `1` | 正準形 |
| `[[1]][[2]]` | `1,2` | 正準形 (2 マッチ) |
| `[[1], [5]]` | `1,5` | 追加 |
| `[[1],[5]]` | `1,5` | 追加 (空白なし) |
| `[[1], [5], [9]]` | `1,5,9` | 追加 (3 件以上) |
| `[[1], [5]][[7]]` | `1,5,7` | 追加 (連続形との混在) |
| `[[1]], [[5]]` | `1,5` | 現行どおり 2 マッチ、`, ` は本文に残る |
| `[[1, 5]]` / `[[1,5]]` | なし | 内側カンマ形は**受理しない** (設計判断 3) |
| `[[1] , [5]]` / `[[1 ], [5]]` / `[[1], [ 5]]` | なし | 空白を許すのは `,` の直後だけ |
| `[[1],\n[5]]` | なし | 改行は区切りに含めない |
| `[[1,]]` / `[[, 5]]` | なし | 空 ref は非受理 |
| `[[a]]` / `[[１２]]` / `[[12]` | なし | 現行どおり非受理 |

空白を許す位置を `,` の直後だけに絞る理由は 2 つある。1 つは、`]` の前後にまで
空白を許すと `[[1 ]]` を受理しない単体形と非対称になり、状態機械が数字の後に
空白を先読みして分岐を決める必要が出るため。もう 1 つは、区切りを `\s*` に広げると
改行をまたぐ marker を backend だけが受理してしまうため。frontend では marker が
改行をまたぐと `remarkBreaks` により text node が分割され `findAndReplace` が
成立しないので、層間で不一致になる。

### 3. 内側カンマ形 (`[[1, 5]]`) は受理しない

実際に観測されたのは `[[1], [5]]` の 1 形式であり、そこに絞る。

内側カンマ形まで広げると、ネストした数値配列リテラルとの衝突が実用上の水準に
達する。`行列は[[1, 2], [3, 4]]です` は refs `1,2,3,4` として抽出される (実測)。
evidence answer の本文パースは fenced code を含む本文全体を走査するため、
コードや数値表現を含む回答で誤検知が起きる。落ち方自体は
`_validate_draft_citations` の unknown ref 検出による fail-loud + repair retry
なのでサイレントではないが、可用性を削る方向の副作用になる。

`], [` 区切りだけに絞れば `[[1, 2], [3, 4]]` は非マッチになる (実測)。残る衝突は
`[[1], [2]]` のような 2x1 のネスト配列リテラルだけで、頻度は桁で下がる。この残余
リスクは受け入れる。

### 4. 加算的であることを実測で確認済み

既存テストのマーカーフィクスチャ 21 パターンで新旧の ref 抽出結果と除去結果を
突き合わせ、**差分ゼロ**を確認した。`[[1]][[2]]` が 1 グループに飲み込まれない
のは、区切りの後に必ず数字を要求するため。

したがって既存テストは全て通り、破壊されるのは上記の禁止行 substring テスト
1 件のみ。

### 5. 構文の SSoT を 1 箇所に置く

`backend/app/agent/citation_markers.py` を新設し、`answering/` と `runs/` の双方が
そこから import する。両者は「citation marker 構文」という同一の契約を持ち、
変更理由も同一 (構文が変わったとき) なので括り出しの条件を満たす。配置を
`app/agent/` 直下にするのは、`answering/` → `runs/` / `runs/` → `answering/` の
どちらの向きにも新しい依存を作らないため。

公開する関数は 2 つ。

```python
def parse_citation_refs(text: str) -> tuple[str, ...]:
    """本文から citation marker の ref を初出順・重複排除で取り出す。"""

def strip_citation_markers(text: str) -> str:
    """本文から citation marker を除去する。"""
```

`visible_text.py` の状態機械は増分入力を扱うため正規表現を共有できない。構文の
一致は**共有テストコーパス**で担保する (Tests 節)。frontend も同じ理由で独立実装に
なるため、同一コーパスを写す。

### 6. 状態機械の拡張 (`visible_text.py`)

既存 5 状態に 1 状態 (`REF_SEPARATOR`) を足し、既存の `DOUBLE_OPEN` を `REF_OPEN`
へ改名する。追加・変更する遷移だけを書く。

| 状態 | 入力 | 遷移 |
|---|---|---|
| `CLOSE` | `,` | → `REF_SEPARATOR` (候補に追加) ※追加 |
| `REF_SEPARATOR` | 空白 / タブ | → `REF_SEPARATOR` (候補に追加) |
| `REF_SEPARATOR` | `[` | → `REF_OPEN` (候補に追加) |
| `REF_SEPARATOR` | 上記以外 | 候補全体を literal 出力、`TEXT` へ (現在文字は再処理) |

`DIGITS` は変更しない (数字 → `DIGITS`、`]` → `CLOSE`、それ以外 → literal
フォールバック)。空白を許す位置を `,` の直後だけに絞った結果、数字の後で先読みが
不要になり、追加は `CLOSE` の 1 遷移と新規 1 状態だけで済む。

`REF_OPEN` は「ref の開き括弧を読み終え、数字を待つ」状態を表す。`[[` の 2 つ目の
`[` を読んだ直後と、グループ区切り `], [` の `[` を読んだ直後は同じ契約になるため、
`DOUBLE_OPEN` を一般化して 1 状態に畳む。失敗時のフォールバックも共通で、
**候補末尾の `[` を残して手前までを literal 出力し、候補を `[` に戻して `OPEN` へ
戻る**(現在文字は再処理)。

このフォールバックが「候補全体を literal 出力」ではいけない理由:
`前[[1], [[2]]後` では、不正なグループ候補 `[[1], [` の末尾 `[` が後続 marker
`[[2]]` の開き括弧を兼ねる。候補全体を literal にすると `[[2]]` を認識できず、
**既存の正しい marker が除去されなくなる**(加算性が破れる)。既存の `DOUBLE_OPEN`
が `A[[[1]]B` → `A[B` のために持っていた挙動と同じ理由であり、一般化してもこの
互換は保たれる。この形は共有コーパスの `failed-group-overlapping-single` /
`failed-group-overlapping-group` で固定する。

既存遷移は 1 つも変えない。特に `CLOSE` で `]` を読んだときの「marker 完成 =
破棄」は最優先のまま残すため、`[[1]], [[5]]` は従来どおり 2 つの独立 marker として
扱われ、`, ` が本文に残る。グループ経路に入るのは `]` が 1 つだけ続いた場合のみで、
曖昧さは生じない。

### 7. frontend は 1 マッチから複数バッジを返す

`remark-citation-markers.ts` の replace 関数を、group(1) から取り出した ref 列に
対して以下を返すよう変える。

- link / linkReference の内側 (`isInsideLink`): `null` (グループ全体を除去)
- それ以外: `matchableRefs` に含まれる ref のバッジノードだけを配列で返す。
  1 件も含まれなければ空配列 (= グループ全体を除去)

区切り文字 (`, ` など) はバッジ間に残さない。連続形 `[[1]][[2]]` の描画と揃える。

### 8. API 契約の description にグループ形を追記する

`ResearchAssistantMessage.content` の Field description は marker 形式の SSoT
であり、OpenAPI 経由で `types.gen.ts` の JSDoc まで到達する。受理構文が変わる
以上、ここを据え置くと契約と実装がずれる。

```diff
     content: str = Field(
         description=(
             "Generated answer text. Evidence-grounded answers may include inline "
-            "citation markers like [[1]], where the number matches sources[].sourceRef."
+            "citation markers like [[1]] or [[1], [2]], where each number matches "
+            "sources[].sourceRef."
         )
     )
```

`/gen-types` で `types.gen.ts` を再生成する。`test_router_research.py:2575` は
`"[[1]]"` の substring を要求しているだけなので、この追記でも通る。

### 9. 残余の誤検知は受け入れる

`], [` 区切りに絞った結果、`[[1, 2], [3, 4]]` のような一般的なネスト配列リテラルは
非マッチになる (実測)。残るのは `[[1], [2]]` のような 2x1 のネスト配列リテラルとの
衝突だけで、以下の理由から許容する。

- **frontend**: `findAndReplace` は text node のみを走査し、`code` (fenced) と
  `inlineCode` は text node ではないため走査対象外。コードとして記述された配列は
  安全で、地の文に裸で書かれた場合のみ露出する。
- **direct answer の除去経路**: この経路はプロンプトで marker 出力自体を禁止して
  いるため、実質的な露出は無い。
- **evidence answer のパース経路**: 本文全体 (fenced code を含む) を走査するため
  露出する。ただし存在しない ref は `_validate_draft_citations` が
  `EvidenceAnswerDraftInvalidError` で弾き、repair プロンプト付きで 1 回 retry
  される。**サイレントには壊れず、fail-loud + 自己修復の経路に乗る**。

fenced code 除外は今回やらない (Non-goals)。本番で実際に発生した場合の
follow-up とする。

## プロンプト変更 (確定形)

`evidence_answer/prompts.py` の「# 引用」節から禁止行 1 行だけを削除する。

```diff
 - evidenceに基づく主張の直後に `[[source_ref]]` を付ける。
 - evidenceに存在しないsource_refは使用しない。
 - 複数の出典を引く場合は `[[1]][[2]]` のように連続して書く。
-  `[[1], [2]]` の形式は使わない。
 - SourcesやReferencesの一覧は作らない。
 - citation markerは見出しに付けない。
```

`EVIDENCE_ANSWER_PROMPT_VERSION` を `v6` → `v7` に上げる。
`direct_answer/prompts.py` は変更しない (marker 出力を禁止する指示のままで、
受理側が広がっても意味は変わらない)。

## Invariants

- 正準形は `[[N]]` のまま。プロンプトが推奨する形は `[[1]][[2]]` のまま。
- 既存形式 (`[[1]]` / `[[1]][[2]]` / 不正形の literal 残留) の挙動は 1 つも
  変わらない。受理は加算のみ。
- `parse_citation_refs` が返す ref 列は初出順・重複排除。
- 1 つのグループから取り出した ref 列は、書かれた順にバッジ化される。
- `validation.py` と `citation_integrity.py` は同一の構文実装を共有し、
  「片方だけがグループ形を認識する」状態を作らない。
  - この不変条件が破れると、cited_refs に載った ref が
    `source_without_marker_refs` として偽 warning を出し続ける。
- `visible_text.py` の状態機械と `citation_markers.py` の正規表現は、同一の
  テストコーパスに対して同一の可視テキストを返す。
- frontend の受理構文は backend と一致する。片側だけが広いと、バッジ化されない
  marker が生テキストで残る / 出典が sources に載らない、のいずれかが起きる。
- グループ内に evidence に存在しない ref が 1 つでもあれば、draft は従来どおり
  `EvidenceAnswerDraftInvalidError` で弾かれる (グループ単位での握り潰しはしない)。
- `ResearchAssistantMessage.content` の description が示す marker 形式は、実装の
  受理構文と一致する。

## Non-goals

- 正準形の変更、および既存 spec / test に散在する `[[1]][[2]]` リテラルの書き換え。
- 内側カンマ形 `[[1, 5]]` の受理 (設計判断 3)。
- fenced code / inline code を backend パースの対象外にすること。
- 区切りに改行を含めること、および `,` の直後以外の位置に空白を許すこと。
- `[[1]] [[5]]` (空白区切り) のバッジ間空白を詰めること、およびバッジ間に区切り
  記号を導入すること。
- `[[1]], [[5]]` の本文に残る `, ` を除去すること (既存挙動の維持が優先)。
- direct answer 側のプロンプト変更。

## Changed Files

### backend (本番コード)

| ファイル | 変更 |
|---|---|
| `app/agent/citation_markers.py` | **新規**。構文の SSoT。`parse_citation_refs` / `strip_citation_markers` |
| `app/agent/answering/evidence_answer/validation.py` | `_CITATION_MARKER_RE` / `_citation_refs_from_answer` を削除し、`parse_citation_refs` へ委譲 |
| `app/agent/runs/citation_integrity.py` | `_CITATION_MARKER_RE` / `_citation_refs_from_answer` を削除し、`parse_citation_refs` へ委譲 |
| `app/agent/answering/direct_answer/flow.py` | `_CITATION_MARKER_RE` を削除し、`strip_citation_markers` へ委譲 |
| `app/agent/answering/visible_text.py` | `_MarkerState` に `REF_SEPARATOR` を追加、`DOUBLE_OPEN` を `REF_OPEN` へ改名、`CLOSE` の遷移 1 本追加 |
| `app/agent/answering/evidence_answer/prompts.py` | 禁止行 1 行削除、`EVIDENCE_ANSWER_PROMPT_VERSION` を `v7` へ |
| `app/schemas/research.py` | `ResearchAssistantMessage.content` の description にグループ形を追記 |

### frontend (本番コード)

| ファイル | 変更 |
|---|---|
| `src/features/research/markdown/remark-citation-markers.ts` | `CITATION_MARKER_PATTERN` 差し替え、replace 関数を配列返却へ、doc コメント更新 |
| `src/types/types.gen.ts` | `/gen-types` による再生成 (手動編集しない) |

### spec

| ファイル | 変更 |
|---|---|
| `backend/specs/question-answering-inline-citation-slice.md` | parse 規則 (`:40`, `:126`) に受理拡張への参照を追記 |
| `backend/specs/question-answering-flow-boundary-refactor-slice.md` | `:424` の凍結契約に「正準形は不変、受理構文は本 slice で拡張」を追記 |
| `frontend/specs/research-final-answer-markdown.md` | `:82` / `:146` の marker 定義を更新 |
| `frontend/specs/research-live-draft-markdown.md` | `:78` の正規表現記述を更新 |

## Tests

### 共有テストコーパス

`backend/tests/agent/citation_marker_corpus.py` を新設し、
`(入力, 期待 refs, 期待除去結果)` の組を 1 箇所に定義する。以下 3 つがこれを
parametrize で共有し、構文実装の drift を構造的に検出する。

1. `tests/agent/test_citation_markers.py` (正規表現)
2. `tests/agent/answering/direct_answer/test_stream_filter.py` (状態機械、
   一括投入 / 1 文字ずつ投入の両方)
3. frontend `remark-citation-markers.test.tsx` (同じ組を
   `src/features/research/markdown/citation-marker-corpus.ts` へ写す)

frontend は plugin を直接 import せず `react-markdown` 経由で描画し、バッジを
テキストを持たない probe 要素に差し替えることで ref 列と本文を読み取る。
`unified` / `remark-parse` は未宣言の推移的依存なので import しない。

コーパスの `refs` は初出順・重複排除であり、plugin は出現ごとにバッジを作る。
比較時はバッジ列を初出順で重複排除するため、**同一グループ内の重複 ref が何個の
バッジになるか**はコーパスでは固定できない。これは専用テストで別に固定する。

コーパスに含める最小セット:

- 既存形: `[[1]]` / `[[1]][[2]]` / `[[2]][[1]] ... [[2]]` / `[[1]] [[5]]` /
  `[[1]], [[5]]` / `A[[1]]][[2]]B` / `[[[[1]]]]` / `A[[[1]]B`
- 非受理形: `[1]` / `[[a]]` / `[[１２]]` / `[[12]` / `[[` / `[[12` /
  `[[internal-1]]` / `[[1,]]` / `[[, 5]]` / `[[1],\n[5]]` /
  `[[1] , [5]]` / `[[1 ], [5]]` / `[[1], [ 5]]`
- 追加形: `[[1], [5]]` / `[[1],[5]]` / `[[1],  [5]]` / `[[1], [5], [9]]` /
  `[[1], [5]][[7]]`
- 非受理を固定する誤検知形: `[[1, 5]]` / `[[1,5]]` / `行列は[[1, 2], [3, 4]]です`
  (いずれも refs 0 件・本文無変化であることを明示的に固定する)

### backend 個別

- `test_validation.py`: グループ形からの ref 展開、初出順 dedupe、連続形との混在、
  グループ内に不実在 ref があるとき `EvidenceAnswerDraftInvalidError`、
  グループ形のみでも「marker 0 件」判定にならないこと
- `test_citation_integrity.py`: グループ形の本文と対応 sources で
  `has_mismatch` が `False` になること (偽 warning の回帰テスト)
- `test_stream_filter.py`: chunk 境界 (`[[1],` / ` [5]]`、`[[1], [` / `5]]`) を
  またぐ除去、未完成グループ (`[[1],` で入力終了) の literal フォールバック、
  `finish()` の扱い
- `test_flow.py` (direct answer): グループ形の除去、除去後が空白のみなら
  `DirectAnswerInvalidError`
- `test_prompt_schema.py`: 禁止行の parametrize エントリを削除、
  「連続して書く」行の固定は維持、version 定数一致テストは既存のまま
- `test_router_research.py`: OpenAPI description の substring 固定 (`:2575`) が
  追記後も通ること

### frontend 個別

- `remark-citation-markers.test.tsx`: コーパス全ケース、片方だけ sources にある
  場合は一致分のみバッジ化、1 件も一致しなければグループごと除去、link 内側の
  グループは全除去、inline code / code fence 内は非置換
- `remark-citation-markers.test.tsx`: 同一グループ内の重複 ref
  (`[[1], [1]]`) が **バッジ 1 個** になること。コーパスでは固定できないため専用に置く
- `CitedAnswerContent.test.tsx`: グループ 1 個 → バッジ 2 個 (順序保持)、
  バッジ間に区切り文字が残らないこと
- `LiveAnswerDraft.test.tsx`: draft ではグループ形も literal 表示のままである
  こと (既存の非バッジ化契約の維持)

## Done

- 上記 Changed Files の本番コード 8 ファイル (backend 7 + frontend 1) が
  変更済みで、`types.gen.ts` が `/gen-types` で再生成済み。
- 共有コーパスが 3 実装 (backend 正規表現 / backend 状態機械 / frontend) で
  parametrize され、全て green。
- 既存のマーカー関連テストのうち、Tests 節が要求する追記・更新以外は無修正で
  green。プロンプト禁止行の substring 固定 (`test_prompt_schema.py`) と
  prompt version の期待値 (`test_prompt_schema.py` / `test_agent_declaration.py`)
  だけが既存アサーションの変更にあたる。
- `/check` が backend / frontend の両方で green。
- spec 4 ファイルの記述が実装と一致している。
