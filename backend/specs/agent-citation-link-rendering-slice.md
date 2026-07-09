# 引用 marker のリンク化 slice 仕様（表示改善 + 保存時観測）

## 位置付け

親仕様: `specs/agent/conversation-history-async-runs.md` 系列の表示改善 slice。
Slice 3（thread UI）の後、Slice 4（progress_stage）の前に行う。
**frontend 表示改善 + backend 保存時観測。API / DB / 生成型の変更なし。**

## Problem

回答本文に引用 marker `[[3]]` が生テキストのまま表示されている。読み手には
意味不明の記号であり、出典への導線にもなっていない。marker を本文表示から消し、
「触れると出典のプレビューが出て、その中の明示的なリンクからジャンプできる」
表示に置き換える。

同時に、`marker ↔ sources` の不整合は backend regression として保存境界で
検知・記録する。frontend は契約の消費者なので、runtime 通知チャネルは持たず、
表示上は対応 source の無い marker を黙って除去する。

## Evidence（調査済みの前提事実）

- **marker ↔ sources の整合は backend が生成時に構造保証済み**:
  - `synthesis.py:369-375` — marker 群から cited_refs を再計算（乖離時は marker が正）。
  - `synthesis.py:436-447` / `service.py:107` — evidence に存在しない ref の marker は
    `AnswerDraftInvalidError` で fail-loud（保存に至らない）。
  - `service.py:140-146` — 保存される sources は cited のみ。
  - → **通常経路では保存済み回答に「対応 source の無い marker」は発生しない**。
    発生 = backend regression であり、検知責務は backend。
- **保存境界には最後の網がまだ無い**:
  - `AgentHistoryRepository.complete_run` が assistant message と source rows を同一 tx で
    保存し、run を completed に遷移させる決定境界。
  - 生成時検証を通った後に mapper / repository / 将来変更で不整合が入り込んだ場合、
    現状は保存時点で確定的に検知されない。
  - この slice で `complete_run` 保存 tx 内に純関数チェックを足し、run は落とさず
    structured warning として記録する。
- **契約 SSoT**: `ResearchAssistantMessage.content` の Field description
  （`[[N]]` ↔ `sources[].sourceRef`、生成型 JSDoc に届く。C-3 確定事項）。
  frontend が content を parse する設計は合意済み。
- direct 回答（sources 空）は marker を含まない設計（含んだ場合の挙動は設計判断 4 の帰結）。
- source カード先頭に sourceRef の番号バッジが既にあり（`ResearchThreadView.tsx:101`）、
  同じ視覚語彙を本文バッジに再利用できる。
- `components/ui/` に Popover / HoverCard / Tooltip は**未導入** — 追加は shadcn CLI
  経由（`components/ui/` は手動編集禁止領域）。
- 本文は `whitespace-pre-wrap` で描画されるため、parse は改行・空白を保持したまま
  全文を token 分割して React node 化する必要がある。

## 設計判断

1. **marker は本文表示から除去し、上付きの数字バッジに置換**する（sourceRef の番号。
   source カードの番号バッジと同じ見た目）。本文テキストのデータ自体は改変しない
   （表示時の置換のみ）。
2. **バッジ直クリックでは遷移しない**。hover（pointer デバイス）または tap（touch）で
   出典プレビューを表示し、**遷移はプレビュー内の明示リンクのクリックのみ**
   （ユーザー決定: 誤タップで飛ばされない、まず何のソースか確認できる）。
   - プレビュー内容: kind（内部記事 / 外部）、タイトル、source_name・published_at、
     external は evidence_claim（引用文）。
   - 遷移先: external = URL を新タブ（`rel="noreferrer"`）/ internal = `/news/{articleId}`。
   - internal で `articleId` が null（記事削除後）はリンクを置かずタイトル等のみ表示。
   - 実装コンポーネントは shadcn Popover 系を CLI 追加して使う（hover/tap 両対応の
     具体構成は frontend-ui-builder に委ねるが、上記のインタラクション要件が正）。
3. **source カードは変更しない**。カードへの in-page ジャンプ案は不採用
   （プレビュー方式に置換。カード側の遷移リンクは従来どおり）。
4. **対応 source の無い marker は表示から除去する**（ユーザー決定）。整合性は
   backend が生成時に構造保証し、保存時にも regression を warning 記録する。
   frontend で可視化・runtime 通知はしない。帰結として sources が空の回答
   （direct）に marker 様文字列があればすべて除去される。
5. **backend の検知は保存境界 `complete_run` で行う**。
   - answer から marker refs を抽出し、保存する source rows の `source_ref` 集合と照合する。
   - 不一致は `structlog.warning`（Logfire に集約される）で記録し、run は落とさない。
     回答本文自体はユーザーに価値があり、これは表示品質・契約 regression の観測であって
     回答失敗ではない。
   - log event は `agent_citation_source_mismatch`。属性は `run_id`,
     `marker_without_source_refs`, `source_without_marker_refs` のみ。
     answer 本文・URL・title・evidence_claim は載せない。
   - `pipeline_events` には焼き込まない。現時点で分離した consumer が無く、監査 SSoT に
     昇格させるほどの domain event ではない。
6. **parse は純関数**（`/\[\[(\d+)\]\]/`）として切り出し、単体テストで固定する。
   連続 marker `[[1]][[2]]`・句読点隣接・行頭行末・複数桁・marker なし素通しを含む。

## 対象 / 構造

```text
frontend/src/features/research/components/
  CitedAnswerContent.tsx     (新規: parse 純関数 + 本文描画。assistant content 置換先)
  SourcePreviewBadge.tsx     (新規: バッジ + プレビュー popover)
  ResearchThreadView.tsx     (AssistantMessage の content 描画を CitedAnswerContent へ)
frontend/src/components/ui/  (shadcn CLI で popover 系を追加)
backend/app/agent/history/
  citation_integrity.py      (新規: marker/source_ref 照合の純関数)
  repository.py              (complete_run 保存 tx 内で warning 記録)
```

user message・missingAspects・source カードは対象外。

## Invariants

- content のデータは改変しない（表示置換のみ。空白・改行は `whitespace-pre-wrap` の
  まま保持）。
- API / DB / `*.gen.ts` に変更なし。
- backend の検知は保存境界で warning 記録のみ。run status / response shape /
  永続化 payload は変えない。
- telemetry に answer 本文・URL・title・evidence_claim・user question を載せない。
- features 境界維持・`components/ui/` は CLI 追加のみ。
- a11y: バッジに `aria-label="出典 N"`、プレビューはキーボード到達可能。

## Non-goals

- frontend から backend への不整合通知 API 追加。
- 不整合時に run を failed にすること。
- `pipeline_events` / DB に citation mismatch を永続化すること。
- source カード側の変更・ハイライト。
- marker 記法自体の変更（C-3 契約は不変）。
- progress 表示（Slice 4）・Redis イベント（Slice 5）。

## Tests

1. parse 純関数: 単一 / 連続 / 複数桁 / 句読点隣接 / 行頭行末 / marker なし /
   unmatched 除去 / sources 空で全除去。
2. コンポーネント: バッジが本文に置換表示される / プレビューに kind・タイトル・
   evidence_claim が出る / external リンクが新タブ + noreferrer / internal リンクが
   `/news/{articleId}` / articleId null でリンク非表示 / unmatched marker が
   描画されない。
3. backend 純関数: marker/source_ref 一致 / marker_without_source_refs /
   source_without_marker_refs / 重複 marker dedupe / marker なし + source あり。
4. repository: `complete_run` で不一致時に本文・URL・title・evidence_claim を含まない
   warning が 1 回記録され、run は completed のまま保存される。
5. 既存の research コンポーネントテストが green のまま。

## 検証の制約

- `/check`（frontend 分: biome / tsc / vitest、backend 分: ruff / pytest の対象テスト）。
- UI 実確認は dev で既存 completed データまたは seed（dev は LLM 生成不可）。

## Done

- 本文から `[[N]]` の生テキストが消え、番号バッジとして表示される。
- hover/tap でプレビュー → プレビュー内リンクから遷移できる（external 新タブ /
  internal 記事ページ / 削除済み記事はリンクなし）。
- 対応 source の無い marker は表示されない。
- 保存境界で marker/source_ref 不整合が structured warning として記録され、run は
  completed のまま維持される。
- テスト green + 既存 suite green。
