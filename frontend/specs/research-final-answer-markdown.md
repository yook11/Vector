# Research 確定回答 Markdown 描画仕様

> 作成日: 2026-07-23
>
> Status: Approved（方針・新規 dependency とも 2026-07-23 承認済み・実装未着手）
>
> 対象: Research 確定回答（`ResearchAssistantMessage.content`）の frontend 表示のみ

## 位置付け

Research 回答の Markdown 対応は次の3段で進め、本仕様は第1段だけを扱う。

1. 確定回答の Markdown 描画（本仕様）
2. SSE live draft の Markdown 描画（別仕様）
3. プロンプト緩和（evidence prompt の「Markdown rendererに依存せず」の変更と、
   direct / evidence 双方への Markdown 利用ルール設定。別仕様）

第3段を先行させると、SSE 中は `## 見出し` や `**強調**` が生文字で見え、確定時だけ装飾される
表示差が生まれる。生成側の切替は両 renderer の準備後に行う。

`backend/specs/agent-evidence-answer-composition-slice.md` は「frontendの現行回答表示は改行を
保持するがMarkdownを解釈しない」ことを前提に Markdown 描画を scope 外としていた。本仕様は
その除外されていた frontend 改善にあたる。backend API・Pydantic schema・DB・プロンプトは
変更せず、frontend 内で完結する。

`frontend/specs/agent-research-live-ui-slice.md` が定める live 表示（draft、進捗、stable answer
slot、auto-follow）の契約は本仕様で上書きしない。

## Work Definition

### Problem

確定回答は `whitespace-pre-wrap` のプレーンテキストとして描画され、回答本文に含まれる
Markdown 構文（見出し・リスト・強調・コードなど）が生文字のまま表示される。evidence 回答は
プロンプトで renderer 非依存の書式を指示しているが、direct 回答には Markdown を禁止する指示が
なく、装飾記号が生で見える回答が生まれ得る。構造を持つ長文回答の可読性が損なわれている。

### Evidence

| 対象 | 現状 |
|---|---|
| 確定回答経路 | `ResearchThreadView` → `ResearchTurn` → `ResearchRunAnswerSlot`（`finalAnswer !== null` 時）→ `ResearchAnswerSlot` → `CitedAnswerContent` |
| 確定回答の表示 | `ResearchAnswerSlot` の `whitespace-pre-wrap break-words text-sm leading-7` コンテナ |
| `CitedAnswerContent` | Markdown parser ではなく `/\[\[(\d+)\]\]/g` で本文を分割し、`SourcePreviewBadge` へ置換するだけ |
| 固定済み挙動 | `CitedAnswerContent.test.tsx` が「マーカー順序保持 / 未対応 `[[N]]` の本文からの除去 / sources 空なら全マーカー除去 / badge の hover・click プレビュー / 内部記事 `/news/{id}` リンク / 削除済み内部記事はリンクなし」を固定 |
| live draft | `LiveAnswerDraft` の別経路でプレーン表示。確定回答だけの切替が可能 |
| ユーザー質問 / missing aspects | `UserMessage` と `ResearchAnswerSlot` 内の別ブロックで、本仕様の対象経路と分離済み |
| プロンプト | evidence prompt は「Markdown rendererに依存せず、短い自然な見出しを独立行に置き、前後を空行で区切る」を指示し、`test_prompt_schema.py` で固定。direct には Markdown の禁止も推奨もない |
| dependency | `frontend/package.json` に Markdown 系ライブラリなし。導入は新規 dependency（Ask First 対象） |

ライブラリの一次情報（2026-07-23 確認）:

- react-markdown v10.1.0（peerDeps `react >= 18`）。デフォルトで raw HTML をエスケープし、
  README に "safe by default (no `dangerouslySetInnerHTML` or XSS attacks)" と明記。
  raw HTML の描画には `rehype-raw` の明示追加が必要で、本仕様では追加しない。
- `defaultUrlTransform` は相対 URL と `http / https / irc / ircs / mailto / xmpp` だけを許可し、
  `javascript:` などの不許可 URL は空文字にする（GitHub と同じ規則）。
- `components` prop のキーは HTML タグ名のみで、text ノードは差し替えられない。text ノード内の
  独自マーカー置換は remark プラグイン + `mdast-util-find-and-replace`（`@user` mention の
  link 化が公式想定例）で行い、`hName` / `hProperties` を付けたノードを `components` で
  差し替えるのが公式に整合する手法。
- remark-gfm v4.0.1 で table / strikethrough / tasklist / autolink literal / footnote が有効になる。
- react-markdown は既定で `img` も描画対象に含み、`urlTransform` は `href` だけでなく `src` にも
  適用される（https の画像 URL は素通しになり、ブラウザが外部取得する）。
- remark-rehype は footnote に `user-content-fn-*` / `user-content-fnref-*` 形式の id を生成する。
  prefix は `clobberPrefix`（既定 `user-content-`、本来は DOM clobbering 対策）で変更でき、
  react-markdown の `remarkRehypeOptions` prop から渡せる。
- `mdast-util-find-and-replace` の `ignore` の既定は空で、置換から除外する親ノード型は明示指定が
  必要。
- bundle 目安: react-markdown 34.1 kB + remark-gfm 9.8 kB（min+gzip、共有依存の重複排除前）。

一次情報:

- [react-markdown README / changelog](https://github.com/remarkjs/react-markdown)
- [remark-gfm](https://github.com/remarkjs/remark-gfm)
- [remark-rehype（footnote id / clobberPrefix）](https://github.com/remarkjs/remark-rehype)
- [mdast-util-find-and-replace](https://github.com/syntax-tree/mdast-util-find-and-replace)
- [remarkjs Discussion #867（hName / hProperties でのカスタム要素化）](https://github.com/orgs/remarkjs/discussions/867)

### Invariants

1. `[[N]]` マーカー規則を維持する。sources に一致する ref はバッジ化、未対応マーカーは本文から
   除去、出現順序を保持する。この規則は Markdown ブロック構造（段落・リスト項目・テーブル
   セル・見出し）の内側でも成立する。link の内側だけは例外で、バッジ化せず除去する
   （引用マーカー契約を参照）。
2. `SourcePreviewBadge` の hover / click プレビュー、外部リンク（`target="_blank"`
   `rel="noreferrer"`）、内部記事リンク、削除済み記事の非リンク表示を変更しない。
3. raw HTML を描画しない。`rehype-raw` を導入せず、HTML 断片はエスケープされたテキストとして
   可視のまま残す（silent drop で内容を隠さない）。
4. `javascript:` 等の危険スキームを href に残さない。`defaultUrlTransform` の既定を維持し、
   独自の許可リスト拡張をしない。
5. 確定回答の描画を起点に外部リソースを取得しない。Markdown 画像は `<img>` として描画せず、
   alt テキストを可視のまま残す。
6. 確定回答の描画が、同一スレッド内の他の回答と DOM id を衝突させない（footnote を含む）。
7. Markdown 構文を含まない既存の保存済み回答が、単一改行・段落を含め従来と同等に読める。
8. stable answer slot・auto-follow・`finalContentKey` による確定回答への置換契約を壊さない。
9. live draft・ユーザー質問・missing aspects・失敗表示の描画を変更しない。
10. backend API・schema・生成 TypeScript 型・DB・プロンプトを変更しない。
11. `"use client"` 境界を現状より広げない（`CitedAnswerContent` は現状どおり client、
    `ResearchAnswerSlot` は server のまま）。

### Non-goals

- SSE live draft の Markdown 描画（第2段・別仕様。不完全 Markdown の補完描画もそこで扱う）。
- プロンプト変更と prompt version / テストの更新（第3段・別仕様）。
- `rehype-raw` / `rehype-sanitize` 構成による raw HTML 描画。
- streamdown の導入。確定テキストのみの描画に不完全 Markdown 補完は不要で、URL 変換の既定が
  react-markdown より寛容なため採用しない。
- code block の syntax highlighting、数式（KaTeX）、mermaid 図。
- 画像の描画（サムネイル表示、proxy 経由の取得を含む）。回答内画像は alt テキスト表示に留める。
- `@tailwindcss/typography`（prose）の導入。スタイルは `components` mapping + 既存 utility で当てる。
- ユーザー質問・missing aspects・エラー文言・thread タイトルの Markdown 化。
- 保存済み回答本文の書き換え・migration。

### Done

- 確定回答の見出し・リスト・テーブル・強調・コードが HTML 要素として描画される。
- `[[N]]` 規則と badge プレビューの既存挙動が、移植したテストで green のまま維持される。
- Markdown 構文を含まない既存回答の表示が従来と視覚同等である（dev の実データで確認）。
- raw HTML の非描画・危険スキーム無効化・画像の外部取得禁止が component test で固定される。
- `/check` が green。

## Rendering Contract

### 対象と非対象

Markdown 描画は `ResearchAnswerSlot` の確定回答分岐（`finalAnswer.content`）だけに適用する。
同 component 内の missing aspects ブロック、`LiveAnswerDraft` の draft 本文、`UserMessage` の
質問本文はプレーンテキストのまま変更しない。

### 方言と改行

- CommonMark + GFM（remark-gfm）を有効にする。
- 単一改行は改行として描画する（remark-breaks）。既存の保存済み回答はプレーンテキスト前提で
  書かれており、evidence prompt も見出しの独立行配置を指示しているため、CommonMark 既定の
  「単一改行を空白へ畳む」挙動では旧回答の視覚同等性を守れない。段落（空行区切り）は通常の
  段落として描画する。
- remark-gfm により footnote 構文も有効になる。プロンプトは footnote を指示しないが、構文が
  有効である以上、同一ページに複数回答を描画するスレッドでは footnote の DOM id / anchor が
  回答間で衝突し得る。`remarkRehypeOptions` の `clobberPrefix` に回答単位の識別子（message
  `seq` 等）を含め、footnote の id を回答ごとに名前空間化する。`clobberPrefix` の公式な目的は
  DOM clobbering 対策であり、複数ブロックの衝突回避はその応用である。

### 引用マーカー

- remark プラグイン（`mdast-util-find-and-replace`）で text ノードの `[[N]]` を検出する。
  sources と一致する ref は `hName` / `hProperties`（data 属性に ref を保持）を付けたノードへ
  置換し、`components` mapping で `SourcePreviewBadge` に差し替える。一致しない ref は
  ノードごと除去する（既存挙動の維持）。
- link / linkReference の内側ではバッジ化しない。`SourcePreviewBadge` は button であり、`<a>`
  の内側に置くと interactive 要素のネスト（HTML 仕様違反）になるためである。
  `mdast-util-find-and-replace` の `ignore` は「スキャン自体のスキップ」であり除去を実現でき
  ないため使わず、replace callback の `RegExpMatchObject.stack`（text ノードの祖先）で link /
  linkReference 配下を判定し、link 内のマーカーは matched / unmatched を問わず本文から除去
  する。「マーカーを通常本文で生表示しない」規則を link 内でも維持する。
- `hName` に使うタグは intrinsic elements から選び、data 属性の有無で通常タグと判別する。
  タグ名は実装時に決定する。
- code fence / inline code の内側は mdast の literal ノードで text 子を持たないため置換対象外と
  なり、マーカーがそのまま表示される。現行の正規表現置換は code 概念を持たず全文を置換して
  いたため、これは本仕様が意図して固定する挙動変更である。プロンプトは citation marker を
  本文の主張直後に置くよう指示しており、code 内マーカーは実質的に発生しない。

### セキュリティ

- raw HTML はエスケープ表示（react-markdown の既定）とし、`skipHtml` による silent drop は
  使わない。
- URL は `defaultUrlTransform` の既定に任せ、`urlTransform` を上書きしない。
- 回答本文中の Markdown link / autolink は `components` mapping で `target="_blank"`
  `rel="noreferrer"` を付与する（`SourcePreviewBadge` の外部リンクと同じ規約）。ページ内
  fragment リンク（footnote の fnref / back-reference など `#` で始まる href）は対象外とし、
  `target` / `rel` を付与しない。
- Markdown 画像（`![alt](url)`）は `<img>` として描画しない。react-markdown は既定で `img` を
  描画対象に含み、`defaultUrlTransform` も https の src を素通しするため、そのままでは回答を
  表示しただけで外部取得が発生し、evidence 経由の prompt injection と組み合わせると流出
  チャネルになる。`components` mapping の `img` を、外部取得を伴わない alt テキストの可視表示へ
  差し替える（`disallowedElements` は alt が属性のため内容ごと消える silent drop になり、
  使わない）。

### タイポグラフィ

- スタイルは `components` mapping で既存 design token（`--vector-ink` 系）と Tailwind utility を
  当てる。カスタム CSS ファイルは作らない。
- 本文は現行の `text-sm leading-7` を基準とする。
- 回答内見出しはページ階層（thread タイトル = `h2`）より控えめな回答ローカルのスケールに丸め、
  `h1`〜`h6` の視覚差は最大3段階程度に抑える。
- 見出しは意味レベルも回答ローカルへ丸める。`components` mapping で `h1`→`h3`、`h2`→`h4`、
  `h3`→`h5`、`h4`〜`h6`→`h6` へシフト・クランプし、回答内見出しがページの `h1` / `h2` と
  同格にならないようにする。
- code block は等幅 + `overflow-x-auto`、テーブルは横スクロールコンテナに入れ、回答パネルの
  横スクロールを発生させない。
- 確定回答コンテナの `whitespace-pre-wrap` は Markdown 描画側の改行契約と二重になるため外す。
  `break-words [overflow-wrap:anywhere]` は維持する。

## Implementation Boundaries

| 責務 | 所有するもの | 所有しないもの |
|---|---|---|
| citation remark plugin | text ノードの `[[N]]` 検出、sources 照合、未対応 ref の除去、ノード化 | 描画・スタイル・preview |
| `CitedAnswerContent` | Markdown 描画、`components` mapping、badge 差し替え、link / 見出し / code のスタイル | slot 配置、draft、missing aspects |
| `ResearchAnswerSlot` | 確定回答分岐のコンテナ style 変更 | live 表示、run 状態 |

- `CitedAnswerContent` は「引用付き回答本文の描画」という責務が変わらないため名前を維持する。
  plugin の名前は実装時に既存語彙との整合を確認して決める。
- 変更対象: `CitedAnswerContent.tsx`、`ResearchAnswerSlot.tsx`、`CitedAnswerContent.test.tsx`、
  `package.json`、`package-lock.json`。`LiveAnswerDraft` / `ResearchThreadLiveBoundary` /
  backend は変更しない。

## Dependencies（2026-07-23 承認済み）

| package | version | 用途 |
|---|---|---|
| react-markdown | ^10.1.0 | Markdown → React 要素の描画本体 |
| remark-gfm | ^4.0.1 | table / strikethrough / autolink 等の GFM 構文 |
| remark-breaks | ^4 | 単一改行の改行描画（旧回答の視覚同等性） |
| mdast-util-find-and-replace | ^3 | citation plugin での text ノード置換（直接 import するため明示追加） |

bundle 影響は合計で約 45 kB（min+gzip、共有依存の重複排除前）。全て ESM only であり、
vitest / Next.js での解決は実装時に `/check` で確認する。

## Implementation Slice

単一 slice で完結する。

1. dependency 追加。
2. citation remark plugin と `CitedAnswerContent` の Markdown 化、`ResearchAnswerSlot` の
   コンテナ style 変更。
3. テスト移植・追加、実データ確認、`/check`。

## Verification

### Component tests

1. 既存 parse 挙動の移植: マーカー順序保持、未対応 `[[N]]` の除去、複数桁 ref、マーカーなし
   本文の素通し、sources 空での全マーカー除去。
2. 既存 badge 挙動の維持: hover / click プレビュー、外部リンク属性、内部記事リンク、削除済み
   記事の非リンク。
3. 見出し・リスト・テーブル・強調・inline code・code fence が対応する要素として描画される。
4. リスト項目・テーブルセル内の `[[N]]` がバッジ化される。
5. code fence 内の `[[N]]` は置換されず文字のまま表示される（挙動変更の固定）。
6. `<script>` 等の raw HTML が要素化されず、エスケープされたテキストとして残る。
7. `[link](javascript:alert(1))` の href が無効化される。
8. 単一改行を含むプレーンテキスト回答が改行を保って描画される。
9. 回答内 Markdown link に `target="_blank"` `rel="noreferrer"` が付く。
10. Markdown 画像が `<img>` として描画されず、外部リクエストが発生せず、alt テキストが可視で
    残る。
11. link テキスト内の `[[N]]` はバッジ化されず本文から除去され、`<a>` の内側に button が
    生成されない。
12. footnote を含む複数の回答を同一ページへ描画しても DOM id が重複しない（`clobberPrefix`
    の名前空間化）。
13. 回答内見出しの要素が `h3`〜`h6` へシフト・クランプされる。
14. footnote のページ内 fragment リンク（`href` が `#` で始まる anchor）に `target="_blank"` が
    付かない。

### 実データ確認

dev 環境の既存 thread（evidence / direct 双方の保存済み回答）を表示し、旧回答の regression が
ないことを目視確認する。特に、Markdown 意図のないアスタリスク・アンダースコア・`#` が偶発的に
装飾されないかを確認し、問題があれば本仕様の改行・エスケープ方針を見直す。

### 全体

- `/check` を実行し、Biome・TypeScript・component test を通す。
- `npm run build`（production build）を独立の検証項目として明示実行する。導入 dependency は
  全て ESM only であり、モジュール解決の問題は production build でしか顕在化しないため、
  `/check` の内訳に依存させない。

## Acceptance Checklist

- [ ] 確定回答が GFM 込みの Markdown として描画される。
- [ ] `[[N]]` 規則と badge プレビューが Markdown 構造の内側でも既存どおり機能する。
- [ ] raw HTML が描画されず、危険スキーム URL が無効化され、画像の外部取得が発生しない。
- [ ] 複数回答の同一ページ描画で DOM id（footnote 含む）が衝突しない。
- [ ] Markdown なしの既存回答が従来と視覚同等に表示される。
- [ ] draft・質問・missing aspects・失敗表示が変更されていない。
- [ ] backend・API・型生成・DB・プロンプトが変更されていない。
- [ ] `/check` が green。

## 合意済み事項（2026-07-23）

以下は全てユーザー承認済みであり、実装時に再確認を要しない。

1. 新規 dependency 4件（react-markdown / remark-gfm / remark-breaks / mdast-util-find-and-replace）を追加する。
2. remark-breaks を採用し、単一改行を改行として描画する。既存回答は Markdown を前提と
   していないため、この方針で視覚同等性を保つ。
3. code fence / inline code 内の `[[N]]` は置換せず文字のまま表示する（既存の全文置換からの
   挙動変更として受け入れ）。
4. 回答内 Markdown link / autolink は `target="_blank"` `rel="noreferrer"` に統一する。
5. 2026-07-23 のレビュー指摘6件を反映する: Markdown 画像の外部取得禁止（`components.img` で
   alt テキスト表示へ差し替え）、footnote ID の回答単位の名前空間化（`clobberPrefix`）、
   link 内 `[[N]]` の除去、見出しの意味レベルのシフト・クランプ、変更対象への lockfile 追記、
   production build の明示実行。
6. 2026-07-23 の実装時判断2件を反映する: link 内除去の機構は `ignore` ではなく
   `RegExpMatchObject.stack` の祖先判定（`ignore` はスキャンをスキップするため除去を実現
   できない）、ページ内 fragment リンクには `target` / `rel` を付与しない。
