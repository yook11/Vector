# インライン引用 実装 slice 仕様 (Slice C-3)

## 位置付け

evidence 回答 (工程 4E) の answer 本文に `[[N]]` 形式のインライン引用
marker を導入し、「回答のどの文がどの根拠に支えられているか」を
ユーザーに見える形にする。synthesis 工程 (B-1) のプロンプト + 検証層の
変更であり、**API の形 (C-2 契約) は変わらない** — answer 文字列の中身に
marker が増え、`sourceRef` が結合キーとして機能し始めるだけ。

前提 slice: C-2 (API endpoint、**完了済み** — external key 欠落の
fail-fast 503 と `plan_question` 削除まで決着・commit 済み)。frontend の
marker parse 実装より前に完了させる。

## Problem

- 現状の紐づけは回答全体レベル (sources = 引用された根拠のみ) で、
  「どの文がどの根拠から来たか」が分からない。
- 「記事番号ごとに回答を分ける」形式は、統合された答えから遠くなり、
  結論文と根拠の対応がかえって曖昧になるため採らない (合意済み)。

## Evidence

- `backend/app/agent/answering/ai/gemini_prompt.py` — 現行プロンプト。
  文中 marker の指示なし。`cited_refs` を JSON field で申告させている。
  `_NO_EVIDENCE_BLOCK` (evidence 0 件時の指示) あり。
- `backend/app/agent/answering/synthesis.py` — 二層構造の正本:
  `_draft_from_raw` (決定的補完 + defect 記録) /
  `_validate_draft_citations` (不実在 ref → `AnswerDraftInvalidError` →
  retry 対象) / retry 1 回 → fallback。
- 参考一次情報 (ユーザー調査): OpenAI Citation Formatting guide
  (支持される文の末尾に置く・存在しない ID を作らない・複数根拠は全部
  cite)、Perplexity Prompt Guide (References セクションでなく文中)、
  Anthropic Citations (使った箇所に citation を付ける)。

## 合意済みの設計判断

1. **自由文 + インライン citation marker**。marker 形式は **`[[N]]`**
   (N は evidence の `source_ref`)。通常の Markdown link `[text](url)` /
   参照リンク `[text][1]` と衝突せず、`\[\[[0-9]+\]\]` で決定的に parse
   できる。
2. **配置規則**: 根拠に基づく文または節の直後 (句点の後)。複数根拠が
   同じ主張を支える場合は連続配置 `[[1]][[2]]`。References / Sources
   セクションは作らせない。
3. **cited_refs は marker から決定的に導出する** (marker が引用の正本)。
   LLM 申告の `cited_refs` は当面 schema に残し、**照合 (defect 検出)
   にのみ使う**:
   - 申告と marker のズレ (付け忘れ・余分) = **補完可能欠陥** →
     marker から再導出 + defect 記録。retry しない (決定的に修復可能な
     欠陥に LLM retry を使わない — B-1 の二層構造の原則)。
   - ズレ率 (defect) を観測し、低いと分かった時点で `cited_refs` を
     schema から落とす判断をする (将来 slice)。
4. **補完不能欠陥 (retry → fallback)**:
   - marker が evidence に不実在 (`[[9]]` 等) — 捏造引用は直せない。
   - answered なのに marker ゼロ — どの文に付くべきか決定的に決められない
     (既存の「answered → cited_refs 非空」検証の marker 版)。
5. **evidence 0 件の経路**: marker ゼロを指示 (`_NO_EVIDENCE_BLOCK` に
   追記)。insufficient のまま (既存意味論不変)。
6. **sources = marker で実際に使われたものだけ** (cited_refs 導出により
   既存の「引用されたものだけ」写像がそのまま成立)。
7. **direct 経路 (4D) は触らない**。marker は evidence 経路専用。
   direct プロンプトに引用語彙を混ぜない不変条件を維持。
8. **retry 時の `previous_error` に marker 固有のエラー文言**を載せる
   (例:「[[9]] は evidence に存在しない」) — 修復精度を上げる。
9. **`[[N]]` 形式は API 契約の一部で、SSoT は Pydantic field description**。
   `ResearchResponse.answer` に `Field(description=...)` で marker 形式を
   明記し、OpenAPI → generated types (JSDoc) まで届かせる (docstring
   追記だけでは frontend に届かない)。C-2 仕様書にも marker 形式を追記。
   description 変更後は /gen-types で frontend 型を再生成する。
10. **claims[] 中間形式 (文と sourceRefs の構造化 + backend 組み立て) は
    採らない** (draft 契約全体の作り替えになり重い。Non-goals)。
11. evidence 由来テキストに偶然 `[[数字]]` が含まれる誤検出は、
    ニュース QA では無視できるリスクとして**受容する** (明文化)。
12. **insufficient でも根拠に基づく文には marker を付ける** (プロンプトに
    明示)。部分的根拠の紐づけもユーザー価値であり、cited_refs 導出 →
    sources 返却は insufficient でも成立する。「marker 1 件以上必須」は
    answered だけの制約 (既存 validator の意味論どおり)。
13. **marker の重複 (`[[1]]...[[1]]`) は正当な表現**。同じ根拠を複数文で
    引くのは通常の文章であり defect にしない。answer 本文は LLM が書いた
    まま触らず、導出 cited_refs は**初出順 unique** にする。
14. **defect code は `cited_refs_recomputed_from_markers`** (確定)。
    既存の defect 語彙 (`missing_aspects_completed` /
    `blank_cited_refs_removed`) と同じ「何が起きたか」形式。定数の
    所有ファイルは既存 `_DEFECT_*` と同じ **synthesis.py** (audit.py は
    イベント型のみで変更不要)。
15. **marker invariant の保証所在は `AnswerSynthesisService` の出力**
    (工程内の parse + 照合) であり、**`AnswerDraft` 型の validator には
    持たせない**。B-1 で確立した「保証の所在は型ではなく工程内照合」の
    前例どおり。型に本文 parse を持たせると marker 構文が型に結合し、
    strict draft を直接構築する fake (test_service.py) が不正になる。
    `EvidenceAnswerSynthesizer` の返却 draft が marker 整合を満たすことは
    Protocol docstring に契約として明記する。

## プロンプト追加規則 (確定形)

```text
# Citation Rules
- answer 本文では、根拠に基づく文または節の直後に citation marker を付ける。
- marker 形式は [[source_ref]] のみ。例: [[1]]
- citation marker は句点の後に置く。例: 売上は増加しました。[[1]]
- 複数の根拠が同じ主張を支える場合は連続して置く。例: 需要は強いです。[[1]][[2]]
- sufficiency が insufficient の場合でも、根拠に基づく文には citation marker を付ける。
- evidence block に存在しない source_ref を絶対に使わない。
- evidence にない事実を、引用付きの確認済み事実として書かない。
- References / Sources セクションは作らない。
- cited_refs には、answer 本文の citation marker に出した source_ref だけを
  重複なしで入れる。
```

- `_NO_EVIDENCE_BLOCK` に「citation marker を書かない」を追記する。
- repair prompt の新形式は作らない。既存の `previous_error` 機構に marker
  固有のエラー文言を流す。文言には**具体的な marker を含める**
  (例:「answer 本文の citation marker [[9]] は evidence に存在しません」)
  — 修復精度のため。
- 語尾・句読点レベルの微調整は実装内で行ってよい (契約は上記の規則内容)。

## 検証ルール (工程内、二層構造への追加)

| ケース | 分類 | 処置 |
|---|---|---|
| marker が evidence に不実在 | 補完不能 | retry → fallback |
| answered なのに marker ゼロ | 補完不能 | retry → fallback |
| cited_refs と marker の不一致 | 補完可能 | marker から cited_refs を再導出 + defect 記録 |
| evidence 0 件で marker あり | 補完不能 | retry → fallback (不実在 marker と同じ扱いに帰着) |

- marker parse (`\[\[[0-9]+\]\]` 抽出) と cited_refs 導出は
  `_draft_from_raw` (補完層)。
- 不実在 marker 検証は既存 `_validate_draft_citations` の入力が導出後の
  cited_refs になることでほぼそのまま機能する。

## Invariants

- answer 本文中の marker は**必ず sources に解決できる** (決定的検証で
  保証。違反は補完不能欠陥として retry → fallback)。
- 「すべての主張に marker が付く」は LLM 品質であり構造保証しない
  (best-effort。プロンプトで指示し、eval で観測する)。
- answered draft は marker 1 件以上。**保証所在は工程内** (cited_refs を
  marker から導出することで既存 validator が結果的に強制する。
  `AnswerDraft` 型自体は本文 marker を検査しない — 設計判断 15)。
- 補完 (cited_refs 再導出) は defect として必ず記録される (沈黙させない)。
- direct 経路・API schema の形・`AnswerDraft` の strict 契約
  (validator) は変更しない。
- retry / fallback の対象は明示列挙した失敗のみ・想定外例外は伝播
  (B-1 不変条件の維持)。

## Non-goals

- claims[] 中間形式。
- frontend の marker 表示実装 (バッジ / リンク変換は frontend slice)。
- `cited_refs` の schema からの削除 (defect 率を見てから別判断)。
- direct 経路への引用導入。
- 引用品質 (marker の付き漏れ) の eval 整備。

## Changed Files

```text
backend/app/agent/answering/ai/gemini_prompt.py  (Citation Rules 追加 + _NO_EVIDENCE_BLOCK 追記)
backend/app/agent/answering/ai/schema_tool.py    (cited_refs description を marker 一致の説明に更新
                                                  → call signature は自動 bump)
backend/app/agent/answering/synthesis.py         (marker parse / cited_refs 導出 / 検証
                                                  + defect 定数 cited_refs_recomputed_from_markers)
backend/specs/question-answering-research-api-slice.md (marker 形式の契約明記, C-2 側)
backend/app/schemas/research.py                  (answer に Field(description=...) で marker 形式明記)
frontend/src/types/                              (/gen-types 再生成)
backend/tests/agent/answering/test_synthesis.py  (marker 検証テスト追加 + 既存 fixture 全面追従)
backend/tests/agent/answering/ai/*               (プロンプト・schema テスト追従)
```

**追従規模の認識 (想定内のテスト破壊)**: C-3 後は「answered +
cited_refs 申告 + 本文 marker ゼロ」が補完不能 → retry → fallback に
なるため、既存 test_synthesis.py の answered 系 fixture は**すべて本文に
marker を入れる修正が必要**。service 層テスト (test_service.py) の
synthesizer fake は strict `AnswerDraft` を直接返すため影響しない。

## Tests

fake generator で二層構造への追加を検証する。

1. marker 付き valid 出力 → cited_refs が marker から導出され、sources
   写像 (use case 側) が marker 使用分だけになる。
2. 申告 cited_refs と marker の不一致 (付け忘れ) → marker 基準で導出され、
   defect が記録され、処理が止まらない。
3. 申告 cited_refs に余分な ref → 同様に marker 基準 + defect。
4. 不実在 marker `[[9]]` → previous_error 付き retry → 2 回目 valid なら
   採用。
5. 2 回目も不実在 → fallback (insufficient)。
6. answered + marker ゼロ → 補完不能として retry → fallback。
7. evidence 0 件 + marker あり → retry → fallback。
8. evidence 0 件 + marker ゼロ + insufficient → 既存どおり成立 (marker
   導入で壊れない)。
9. marker parse の境界: `[[1]][[2]]` 連続 / 文中位置 / `[1]` (単括弧) は
   marker として扱わない。
10. marker の重複: 同一 ref が本文に複数回出ても defect にならず、導出
    cited_refs は初出順 unique になる。
11. insufficient + marker あり → cited_refs が導出され、use case 写像で
    sources が返る (missing_aspects と共存)。
12. プロンプト: Citation Rules が render に含まれる / insufficient でも
    marker を付ける指示が入る / evidence 0 件時に marker 禁止指示が入る。

## Done

- marker 付き回答が生成され、cited_refs が marker から導出される。
- 不実在 marker / marker ゼロ answered が retry → fallback で
  ユーザーに漏れない。
- 不一致補完が defect として観測できる。
- probe で実 Gemini の marker 付き回答貫通を確認 (実行はユーザー環境)。
- 既存 suite に regression なし。
