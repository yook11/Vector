# Research live draft Markdown 描画仕様

> 作成日: 2026-07-23
>
> Status: Draft（レビュー待ち。新規 dependency remend が Ask First 対象のため実装未着手）
>
> 対象: Research live draft（SSE 蓄積中の `draftText`）の frontend 表示のみ

## 位置付け

Research 回答 Markdown 化3段階の第2段。第1段（確定回答、`research-final-answer-markdown.md`、
PR #50）で確立した描画契約・セキュリティ契約を draft に拡張し、SSE 中と確定後の見た目の差を
「引用バッジの有無」だけに縮める。第3段（プロンプト緩和）は本仕様の後に別仕様で行う。

`agent-research-live-ui-slice.md` が定める live 表示の契約（draftMode、status 文言、failure、
auto-follow、stable answer slot）は本仕様で上書きしない。変更するのは draft 本文の描画方式のみ。

## Work Definition

### Problem

draft は `whitespace-pre-wrap` のプレーンテキスト表示のため、確定時に Markdown 装飾へ一斉に
切り替わり、SSE 中は `**` や `##` が生文字で見える。第3段でプロンプトに Markdown を許可する
前提として、draft 側の受け皿が必要である。また生成途中の不完全な Markdown（未終端の強調、
書きかけのリンク）を素朴に再描画すると、リテラル→装飾のちらつきが発生する。

### Evidence

| 対象 | 現状 |
|---|---|
| draft 描画 | `LiveAnswerDraft` の `DraftContent` が `draftMode === "visible"` かつ非空のとき `whitespace-pre-wrap` の `<p>` で表示 |
| 更新粒度 | SSE tick ごとに `draftText` 全体を置換再描画（reducer 所有。本仕様では変更しない） |
| 確定側資産 | `CitedAnswerContent` が react-markdown（gfm + breaks + citation plugin）と components mapping（セキュリティ・タイポグラフィ・footnote 名前空間）を所有 |
| 回答サイズ | dev 実測で assistant 回答は 274〜1057 字（数 KB 未満） |

不完全 Markdown の実挙動（2026-07-23、インストール済み react-markdown@10.1.0 +
remark-gfm@4.0.1 + remark-breaks@4.0.0 で実測）:

- ブロック構造（見出し・リスト・code fence）は途中でも安全に描画される。未閉フェンスは
  即 code block になり、閉じるまで後続を飲み込む。クラッシュ・描画不能はゼロ。
- インライン記法（`**` / `` ` `` / `~~` / `[](`）はリテラル→装飾の切替ちらつきが出る。
- table は区切り行が完成した瞬間に段落→`<table>` へ構造が切り替わる。
- 生成途中の URL が GFM autolink により一時的に生きた `<a>` になる。
- `[[1` のような引用マーカー断片はリテラル表示で壊れない。

remend（不完全 Markdown 補完の前処理）の一次情報（同日確認）:

- `remend@1.3.0`。Vercel 公式（vercel/streamdown repo の packages/remend）、依存ゼロ、
  unpacked 24KB、Apache-2.0。streamdown 2.5.0 が内部で使う補完処理の単体切り出しで、
  「unified/remark パイプラインに渡す前に生文字列へ適用する preprocessor」と README に明記。
- 補完対象: bold / italic / inlineCode / strikethrough / links・images / 書きかけ HTML タグ
  除去など。table と code fence は補完しない（fence は react-markdown 側が途中描画できる）。
- option の既定: 型定義に「Options default to `true` unless noted otherwise」と明記され、既定
  false は `inlineKatex` のみ（インストール済み実体の `dist/index.d.ts` で確認）。無効化する
  handler は明示指定が必要。
- 実測の落とし穴: links handler が `[[1` を `[[1](streamdown:incomplete-link)` に変換する。
  **`{ links: false, images: false }` の両方を指定**すると `[[1` が素通しになることを実測確認
  （有効判定が `links !== false || images !== false` のため片方だけでは無効化されない）。
- remark-gfm strikethrough の `singleTilde` 既定は true（インストール済み
  micromark-extension-gfm-strikethrough で確認）。`価格は100~200円、容量は50~100GBです。` が
  確定 renderer で `100<del>200円、容量は50</del>100GB` になることを実測（2026-07-23）。
  対処（`singleTilde: false`）は第1段仕様の合意7として反映済み。
- streamdown 本体は不採用: `urlTransform` 既定が passthrough、rehype-harden 既定が全 prefix
  許可で、確定回答側より寛容なセキュリティ既定になる。依存も16パッケージ増え、描画コンポー
  ネントの二重化を招く。
- 再パースコスト実測: 約 6.6KB の GFM 文書で parse + serialize 平均 9.7ms/回。実回答サイズ
  （〜1KB 台）では tick ごとの全再パースで実用域。

一次情報:

- [vercel/streamdown](https://github.com/vercel/streamdown)
- [remend (npm)](https://www.npmjs.com/package/remend)
- [streamdown security docs](https://streamdown.ai/docs/security)
- [AI SDK: Markdown Chatbot with Memoization](https://ai-sdk.dev/cookbook/next/markdown-chatbot-with-memoization)

### Invariants

1. draft の citation マーカーは文字のまま表示する。マーカーの定義は第1段仕様
   `research-final-answer-markdown.md` と同一（`[[` + 数字 + `]]`、正規表現 `\[\[(\d+)\]\]`）
   とし、断片は「`[[` に数字が続く未完の並び」を指す。バッジ化も除去もしない
   （citation バッジは確定回答のみ。第1段の合意を維持）。
2. セキュリティ契約は確定回答と同一: rehype-raw / skipHtml を導入せず raw HTML はエスケープ
   表示、`defaultUrlTransform` の既定を維持、画像は `<img>` を描画せず alt テキスト表示、
   link は `target="_blank"` `rel="noreferrer"`（ページ内 fragment リンクは除外）。
3. 見出しのシフト・クランプ（h1→h3〜h6）と footnote ID の回答単位名前空間化も確定回答と
   同一契約とし、描画実装を共有する。draft と確定回答の描画差は「引用バッジの有無」のみ。
4. draft の状態遷移と付随表示（`draftMode`、status 文言、failure 表示、recovery 文言、
   `aria-busy`）を変更しない。
5. auto-follow・stable answer slot・確定回答への置換契約を壊さない。
6. remend 前処理は表示専用とし、reducer の `draftText` 状態・確定回答本文・保存データには
   適用しない。
7. backend・API・schema・プロンプトを変更しない。

### Non-goals

- streamdown の導入（上記のとおり不採用）。
- ブロック単位の memo 化・throttle の追加（実測 9.7ms/6.6KB、実回答は数 KB 未満のため不要。
  draft が大幅に長文化する将来変更時に再訪する）。
- 不完全な link / image の補完（`links: false, images: false` のため対象外。書きかけリンクは
  リテラル表示のまま）。
- table の途中描画切替・未閉フェンスの一時的な飲み込み・途中 URL の一時 autolink の抑制
  （実測済みの許容挙動として受け入れる）。
- プロンプト変更（第3段）。
- 確定回答描画の視覚変更（共有化のための内部再構成は行うが、確定側の描画結果は変えない）。

### Done

- SSE 中の draft が確定回答と同じタイポグラフィで Markdown 描画され、確定時の見た目の変化が
  「バッジ化と missing aspects の出現」だけになる。
- 未終端のインライン記法が remend で閉じられ、ちらつきなく装飾表示される。
- `[[N]]` と断片がリテラル表示のまま保たれる。
- セキュリティ契約（raw HTML・URL・画像・link 属性）が draft でも component test で固定される。
- 確定回答側の既存テスト22件が green のまま維持される。
- `/check` + `npm run build` green。

## Rendering Contract

### 対象と非対象

Markdown 描画は `LiveAnswerDraft` の `DraftContent` が表示する `draftText` のみに適用する。
status 行（`回答を生成中…` 等）、`FailureContent`、`回答を準備しています…` 等の待機文言、
`LiveAnswerSlotContent` の分岐構造は変更しない。

### 方式

1. 表示直前に remend で不完全なインライン記法を閉じる。適用原則は「未終端インライン記法の
   補完と、一時的な誤ブロック解釈の防止のみを行い、完成テキストの最終描画が確定回答側と
   食い違う handler はすべて無効化する」。remend の option は `inlineKatex` 以外すべて既定で
   有効なため、全 handler を次のとおり明示指定する。

   | handler | 指定 | 理由 |
   |---|---|---|
   | bold / boldItalic / italic / inlineCode / strikethrough | 有効 | 未終端インライン記法の補完（本仕様の主目的） |
   | setextHeadings | 有効 | 段落直後の `-` / `=` 1文字が一時的に setext 見出しへ化けるちらつきを防ぐ。最終描画と乖離しない |
   | links / images | 無効 | 引用マーカー断片 `[[1` が `[[1](streamdown:incomplete-link)` に化ける（実測）。有効判定が OR のため両方 `false` の指定が必須 |
   | katex / inlineKatex | 無効 | renderer に数式プラグインがなく `$$` は本文文字。補完すると確定側に存在しない文字を draft に足す |
   | singleTilde | 無効 | 共有 config が remark-gfm に `singleTilde: false` を指定するため（第1段仕様 合意7）、誤 strikethrough 自体が発生しない |
   | comparisonOperators | 無効 | エスケープした draft と、しない確定側で最終描画が食い違う |
   | htmlTags | 無効 | 書きかけタグの削除は「silent drop で内容を隠さない」Invariant に反する。リテラル表示で確定側と一致させる |
2. 前処理済みテキストを、確定回答と共有する react-markdown 構成（remark-gfm + remark-breaks +
   セキュリティ / タイポグラフィ / footnote 名前空間の components mapping）で描画する。
   共有構成の remark-gfm は `singleTilde: false` を指定する（第1段仕様の合意7に追従）。
   citation plugin は draft では通さない。
3. 共有は `features/research/markdown/` 配下のモジュールに components mapping と
   remarkRehypeOptions 構成を括り出す形で行う。`CitedAnswerContent` は「共有 renderer +
   citation 差し込み」へ再整理し、公開契約（props・描画結果）は変えない。名前は実装時に
   既存語彙との整合を確認する。

### 許容挙動（実測に基づく既知の一時表示）

以下は生成途中に限った一時状態として受け入れ、抑制機構を追加しない。

- 未閉 code fence: 後続テキストが閉じフェンス到着まで code block 内に表示される。
- table: 区切り行完成時に段落→table へ構造が切り替わる。
- 生成途中の URL: autolink により一時的にリンク化される（スキームは defaultUrlTransform で
  検査済み。クリックしても新規タブで開くだけで現画面は失われない）。
- 描画高さの一時的な縮小: 未閉フェンスの閉鎖（飲み込んだ後続テキストの再フロー）や table への
  構造切替では、tick 間で描画高さが縮み得る。auto-follow・スクロールはこの縮小に耐えること
  （検証は実データ確認で行う）。

## Dependencies（要承認・Ask First）

| package | version | 用途 |
|---|---|---|
| remend | ^1.3.0 | draft 表示直前の不完全インライン記法の補完（依存ゼロ・24KB・Apache-2.0・Vercel 公式） |

## Implementation Boundaries

| 責務 | 所有するもの | 所有しないもの |
|---|---|---|
| 共有 markdown 構成（`features/research/markdown/`） | components mapping、remarkRehypeOptions、タイポグラフィ | citation plugin の適用判断、draft 状態 |
| `CitedAnswerContent` | 共有構成 + citation plugin + sources 照合 | draft |
| `LiveAnswerDraft`（`DraftContent`） | remend 前処理と共有構成での draft 描画 | 状態遷移、status / failure 文言、reducer |

- 変更対象: `LiveAnswerDraft.tsx`、`CitedAnswerContent.tsx`（内部再構成のみ）、
  `features/research/markdown/` 配下、`LiveAnswerDraft.test.tsx`、`package.json`、
  `package-lock.json`。reducer / controller / ResearchThreadLiveBoundary / backend は変更しない。

## Implementation Slice

単一 slice で完結する。

1. dependency 追加（承認後）。
2. 共有 markdown 構成の括り出し（確定側の描画結果不変をテストで確認）。
3. `DraftContent` の Markdown 化（remend 前処理 + 共有構成）。
4. テスト・実データ確認・`/check` + `npm run build`。

## Verification

### Component tests

確定側: 既存 `CitedAnswerContent.test.tsx` 22件が無変更で green（共有化の regression ガード）。

draft 側（`LiveAnswerDraft.test.tsx` へ追加）:

1. draft の見出し・リスト・code fence が要素として描画される（見出しは h3〜h6 へシフト）。
2. 未終端 `**強調` が `<strong>` として描画され、`**` が画面に残らない（remend）。
3. 未閉フェンス内のテキストが code block として描画される。
4. `[[1]]` と断片 `[[1` が文字のまま表示され、バッジ（button）が存在しない。
5. raw HTML が要素化されない・画像が `<img>` にならない・link に `target="_blank"`
   `rel="noreferrer"` が付く（fragment リンクは除外）。
6. status 文言・failure 表示・`aria-busy` の既存テストが green のまま。

### 実データ確認

dev で run を実行し、SSE 中の draft 表示と確定回答への置換を目視確認する（auto-follow・
スクロール位置の維持を含む）。特に、描画高さが縮む tick（未閉フェンスが閉じて後続テキストが
code block から通常段落へ再フローするケース、table への構造切替）をまたいで、auto-follow の
追従とスクロール位置が破綻しない（跳ね・置き去りが起きない）ことを確認する。

### 全体

- `/check`（Biome・tsc・vitest 全体）。
- `npm run build` を独立項目として実行（remend は ESM。解決問題は build でのみ顕在化）。

## Acceptance Checklist

- [ ] SSE 中の draft が Markdown 描画され、確定時の見た目差がバッジの有無だけになる。
- [ ] 未終端インライン記法がちらつかず装飾表示される。
- [ ] `[[N]]` と断片がリテラルのまま保たれ、draft にバッジが出ない。
- [ ] セキュリティ契約が draft でも確定側と同一に固定される。
- [ ] 確定回答側のテスト22件と描画結果が不変。
- [ ] draftMode / status / failure / auto-follow の既存契約が不変。
- [ ] `/check` + `npm run build` green。

## 未確定事項（合意待ち）

1. 新規 dependency remend ^1.3.0 の追加（「方式」の handler 明示構成で使用）。
2. draft では citation plugin を通さず、`[[N]]` をリテラル表示のままとする方針
   （現状の draft 表示とのパリティ。確定時にバッジへ変わる挙動は現状と同じ）。
3. 許容挙動3点（未閉フェンスの飲み込み / table の途中切替 / 途中 URL の一時 autolink）の
   受け入れ。
4. 共有 markdown 構成への内部再構成（`CitedAnswerContent` の公開契約・描画結果は不変）。
