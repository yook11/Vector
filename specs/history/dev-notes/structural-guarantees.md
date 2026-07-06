# 開発メモ圧縮記録 — 構造で守る: 不変条件の対象の拡大 (3月-5月)

個人開発メモ (Claude.ai 対話ログ + ローカルノート、リポジトリ外) の圧縮記録のうち、
「不変条件をランタイムチェックや規約ではなく構造 (型・制約・シグネチャ) に守らせる」という考えが
どう広がったかを 3 月から 5 月まで一本で追う系列。原本のコードやコマンドは転記せず、
当時考えていたこと・決めたこと・その後の帰結だけを言語化して残す。全体の地図は [README.md](README.md)。

## 全体像

弧は **値 (3月 VO) → 物理的不変条件 (3月 CHECK 制約) → 段階の前提条件 (4月 型) → AI 境界 (5月 parse)**。
守る対象が広がるたびに「どこか一箇所を SSoT にして、構造的に伝播させる」という同じ答えに到達している。
docs/ddd-adoption/ の弧 (値→振る舞い→境界→構造→状態) の一次資料群にあたる。
同じ弧の素材がレビュー編にも埋まっており (VO 変換境界・silent-drop・Unit of Work)、本冊末尾に参照を集約した。

## 1. 値オブジェクトとバリデーション一元化 (3/28-30)

- 出発点の意図を自分で明確化した場面が重要: AI が「VO を廃止する話」と読み違えたのを訂正し、
  **「VO を採用して、バリデーションを何箇所も書かなくて済むようにしたい」** が目的だと確定させた。
  手段 (Annotated か RootModel か) より先に目的 (検証の一元化 + 型としての区別) を固定した。
- 解として 3 層構成を確立: RootModel (VO 定義、手書きボイラープレート約 90 行 → 約 10 行) +
  TypeDecorator (DB 境界での自動変換、`cache_ok = True` 必須) +
  type_annotation_map (モデル層は `Mapped[SafeUrl]` と書くだけ)。
  「バリデーションの SSoT を VO 層に置き、ORM 層・モデル層へは構造的に伝播させる」。
- SQLModel の限界を構造的に理解した: カラム型に基本型しか使えない、`Mapped` / `mapped_column` が使えない、
  metadata と registry の分離により cross-base の Relationship が解決できない、`sa_column` への逃げ道が常態化して簡潔さが失われている。
  これを **DeclarativeBase 移行の 5 つの理由として文書化** し、段階的移行を決断 (3/29)。
  「新しいライブラリが一番と思い込んで採用した」という反省も記録している。
- VO 導入基準の確立: 複数パスで使う + 正規化 + 等値性が要る → VO (URL)。
  単一パスのみ → アプリ層検証 + DB 制約で十分 (title)。「1 箇所でしか使わないものに VO はオーバーエンジニアリング」。
- `.value` ラッパーを消して `.root` に統一。「RootModel を採用した以上、その API を実装詳細として隠しても
  守れる範囲はほぼない」という判断。境界層 (TypeDecorator) は `Any` を受けて自分で型検査する。
- エラーメッセージに入力値を含めない (ログへの PII / 悪意ある URL 混入防止)。

帰結: ADR `value_objects_sqlalchemy_migration.md` / `sqlmodel-to-declarative-migration.md` の元になった検討。
docs/ddd-adoption/ 第 2 幕 (02-value-objects) の一次資料はこの月のメモ群。
VO の適用範囲はその後さらに見直され、「regex + 長さだけの制約は Annotated で足りる」方向へ畳む判断も後日行っている。

## 2. 多層防御と DB 制約 (3/28-30)

- 「SQLModel だから CHECK 制約が書けない」は誤解で、単にやっていなかっただけと認識を修正し、`__table_args__` で導入。
- 判断基準の精緻化: **物理的不変条件 (空文字禁止・範囲・列挙・status × approved_at の複合条件) は DB の CHECK、
  文脈依存のビジネスルール (状態遷移の許可、外部依存の検証) はアプリ層**。
  状態遷移ルールと「ある時点のデータ整合性」は別の責務であり、CHECK は後者だけを守る。
- 当初「同じフォーマット検証を 2 回書くのは重複」と考えたが、deep research で
  「アプリ検証と DB 制約の重複は冗長ではなく defense in depth (DB は迂回不能な最終防衛線)」という裏付けを得て方針転換。
  コストがほぼゼロの CHECK は付けて損がない、という現実的な判断に落ち着いた。
- 運用習慣の確立: CHECK 追加前に既存データを違反検出クエリで確認し、違反があればマイグレーション内で修正してから制約を張る。
- enum は PostgreSQL native enum を避け VARCHAR + StrEnum + CHECK (値の削除不可・ADD VALUE のトランザクション制約が理由)。
  StrEnum は DB 問題の解決のために生まれたものではなく、VARCHAR 格納方針と「たまたま噛み合った」という理解も整理した。
- `updated_at` は DB トリガー責任 (経路非依存で必ず更新される)。ORM の `onupdate` は ORM 経由でしか効かない。

帰結: `updated_at` のトリガー責任はその後維持されなかった。4/11 に判断軸そのものが
「機械的な時刻 = トリガー / 業務的な時刻 = アプリ」へ修正され ([レビュー編 6](review-6-side-effects.md))、
以後のテーブルは server_default + アプリ代入へ移行している。CHECK 制約・VARCHAR + StrEnum・
違反検出してから制約を張る運用は現行も標準。

## 3. 型で段階の前提条件を保証する (4/10, 4/16, 4/21)

4 月最大の概念的到達点。3 月の「値の不変条件」がここで「段階・境界の前提条件」へ拡大した。

- 4/21 に自力で言語化: **「他の層にタスクを渡すとき、その層で必要な条件を型で定義し、
  渡す時にその型に変換することで保証する」**。これが "Parse, don't validate" /
  "Make illegal states unrepresentable" という確立された手法と同型であることを後から確認した。
  防御的チェックの削減・シグネチャがドキュメントになる・リファクタ耐性、という 3 つの利点も整理。
- 4/16 の適用例: 「このアプリにおける記事とは、分析する価値のある記事」と型で定義することで、
  各所の条件分岐 (analysis があるか? の防御ゲート) を解消できると気づいた。
- 4/10 の契約による設計: `watched_among` の空集合チェックを Repository 内の防御ではなく
  **呼び出し側の事前条件 (precondition) として docstring に明文化** し、Service 側 1 箇所に判定を集約。
  防御的プログラミングと Design by Contract は哲学の違いであり、後者の方が責務が一貫すると判断。
  空 set の `in_()` が SQLAlchemy で常 false 条件に展開される (クラッシュではなく非効率) ことも実挙動で確認し、
  契約の重みを正確に理解した上で「docstring のみ・assert なし」を選んだ。
  watched_among がそこへ至り、最終的にリソース境界の再設計で消えるまでの経緯は
  [レビュー編 3](review-3-news-split.md) 帰結。

帰結: この「型 = データの形ではなく operation の前提条件」「Stage X に進める条件が ReadyForX という型に閉じる」
というメモは、後の typed-pipeline 設計の中核になり、docs/ddd-adoption/ 第 3 幕以降の一次資料となった。

## 4. AI 境界で型を確定する (5/9)

- AI 応答用スキーマと in-scope 確定型の shape 重複に自分で気づき、「帰ってくるものは型で定義しなくていい。
  返ってきた dict を境界で当てはめて判断すればいい」と整理。判定結果を InScope | OutOfScope の union
  (AssessmentResult) として定義し、中間の公開型を廃止した。
- AI 応答は untrusted input なので境界の parse 関数で検証して確定する。4 月に到達した Parse, don't validate の
  適用先が、この月に AI 境界へ広がった。
- 「名前だけ綺麗で不変条件が壊れたまま」を許さず、InScope の category から OUT_OF_SCOPE を型レベルで除外する
  ところまで同じ変更でやり切ると決めた。詰め替えの瞬間に消える AI の生出力 (raw_category 等) は
  監査値として envelope に残す、という線引きも同時に行った。

帰結: すべて現行コードに着地している。AssessmentResult union、境界の parse 関数、OUT_OF_SCOPE を持たない
InScopeCategory が実装され、中間型は消滅した。

## レビュー編に埋まる同じ弧の素材

- **VO の変換境界** ([レビュー編 2](review-2-routers.md) テーマ 3、4/3): `str()` 手動ラップは TypeDecorator による
  三層防御のバイパス、という発見。「Annotated[Model, Query()] なら VO 可」規律の起点。
- **silent-drop と二型分離の畳み込み** ([レビュー編 4](review-4-params.md) 帰結、4/5): Depends(Model) で
  VO フィールドが黙って落ちる footgun の発見が、「Depends(Model) 禁止」規律を確定させた。
- **Unit of Work** ([レビュー編 6](review-6-side-effects.md) テーマ 3、4/11): トランザクション境界を dependency に
  置き「書き忘れが構造的に不可能」にする——VO で値を守るのと同じ思想の、規約への適用。

## 通底していた思考

1. **SSoT を一箇所に置き、構造的に伝播させる。** 3 月に VO で確立したこの形が、CHECK 制約・型・parse 関数・
   dependency と対象を変えながら繰り返された。
2. **適用範囲の見極めも同時に進化した。** 「1 箇所でしか使わないものに VO はオーバーエンジニアリング」から
   「regex + 長さだけなら Annotated で足りる」まで、守る価値があるかを毎回問い直している。
3. **守る対象が 値 → 段階 → 境界 へ広がった。** この拡大自体が docs/ddd-adoption/ の記録の弧そのもの。

## 関連文書

- docs/ddd-adoption/ — 第 2 幕 (02-value-objects) と第 3 幕 (03-behavior-into-domain) の一次資料がこの系列
- docs/adr/value_objects_sqlalchemy_migration.md / sqlmodel-to-declarative-migration.md
- [レビュー編 4](review-4-params.md) / [レビュー編 6](review-6-side-effects.md) — 同じ弧の適用例
- [pipeline-failures.md](pipeline-failures.md) — 失敗の語彙というもう一つの型設計
