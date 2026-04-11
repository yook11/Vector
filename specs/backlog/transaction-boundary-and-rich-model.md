# Transaction Boundary と Rich Domain Model

> 作成日: 2026-04-11
> 最終更新: 2026-04-12
> ステータス: 議論中（論点1・論点2 決定済み[削除系の rowcount 扱いも (b) で決着]、論点3 を 2軸に分割：3-Y を先行実装、3-X は後続。論点4 は未決）
> 契機: `refactor/news-source-activation` における Repository 層と Model 層の責務境界に関する議論

## 背景

`news_source` の activate/deactivate を実装するなかで、Repository メソッドが
「ドメインルール（is_active を書き換える）」と「永続化（commit する）」を
同一メソッドに抱えていることへの違和感が出発点となった。

議論の結果、これは単なる命名や置き場所の問題ではなく、以下の3つが絡み合った
構造的な論点であることが分かった。

1. **Transaction Boundary をどこに置くか** — 1 HTTP リクエスト = 1 トランザクション
   を構造的に強制する仕組みの設計
2. **Repository の責務をどこまで狭めるか** — commit / refresh / 状態変更をどう剥がすか
3. **ドメイン状態遷移を Model に持たせるか** — Rich Domain Model 化の是非と範囲

本ドキュメントは、これらを論点として整理し、決定事項を段階的に記録する。

## 前提となる原則

> **トランザクションの粒度は「何を原子操作とみなすか」で決まる。HTTP 経路では「1 ユーザー操作 = 1 原子操作」であり、Worker 経路では「1 パイプライン段階 = 1 原子操作」である。両者は性質が異なるため、同じ設計で揃えない。**

この原則は議論全体の最上位に置かれ、後続の全ての判断はここから導出される。

### 補足: なぜ HTTP と Worker を分けるのか

| 側面 | HTTP Service | Worker Service |
|------|------|------|
| 1メソッドの目的 | ユーザー操作1つに応答する | パイプラインの1段階を進める |
| 期待される応答時間 | ミリ秒 - 数秒 | 秒 - 分 |
| 外部 API 呼び出し | 無い、あっても短時間 | 有る、長時間かかる |
| 失敗時の振る舞い | 全部 rollback して 4xx/5xx | 部分進捗を残し再試行可能にする |
| トランザクション粒度 | 1メソッド = 1トランザクション | 1メソッド内に複数トランザクション |
| 整合性の単位 | ユーザー操作の単位 | パイプライン1段階の単位 |

この性質の違いを認めずに同じ構造で設計しようとすると、どちらか一方が
必ず不自然になる。揃えることを美徳としない。

## 論点1: Transaction Boundary をどこに置くか — **決定済み (2026-04-11)**

### 決定

#### HTTP 経路

**セッション配布の dependency 層がトランザクション境界を握る方針を採用する。**

- セッションを配布する dependency は、begin 済みの状態で Service に yield する
- Service 本体はトランザクション境界を自分で書かない。ただ与えられた
  セッションに対してドメイン操作を行うだけ
- リクエストの完了時点で dependency 層が正常なら commit、例外なら rollback を行う
- この結果、「1 Request = 1 Session = 1 Transaction」が構造的に強制される。
  Service 側で書き忘れたり付け忘れたりすることが物理的に起こり得ない

#### Worker 経路（パイプライン用、将来実装）

**Service がセッションの factory を受け取り、必要なタイミングで自分で
トランザクション境界を切る方針を採用する。**

- 1つの Worker Service メソッドの中で複数のトランザクションを意図的に分ける
- 外部 API 呼び出しはトランザクションの外で行う。DB 接続を長時間握らない
- 各段階（fetch, analyze, save 等）は独立したトランザクション単位として扱う
- 失敗時のリカバリ（例: PROCESSING → FAILED の記録）も、本処理とは別の
  独立したトランザクションで行う

#### Repository の責務

- Repository は **commit しない**
- Repository は **refresh しない**（refresh は commit 前提のため）
- 必要に応じて `flush` のみ行ってよい（auto-increment ID 採番などのため）
- session は受け取るが、境界を自分で切らない
- この設計により Repository は HTTP / Worker のどちらから呼ばれても
  同じ動きをする共有資産になる

#### ドメイン例外時の挙動

**例外 = 全て rollback。ドメイン例外（`NotFoundError`, `DuplicateError` 等）も例外ではない。**

- Service メソッドは「成功するか、何もしなかったことになるか」の二値
- Vector の現状の構造（global exception handler が Service の外で例外を
  404/409 に変換している）は既にこの規約と整合している
- 部分 commit が必要になった場合は Service メソッド自体を分割する
- 「失敗しても DB に残したい情報」が必要になったら、その時に別仕組みを
  用意する。当面は不要でアプリログで代替する

### 決定の帰結（注意点）

1. **部分 commit したければ Service メソッドを分ける** — 1つのメソッド内で
   「ここまで確定、ここから rollback 可能」という境界は作れない。必要なら
   Router 側で複数の Service メソッドを呼び分けることで解決する
2. **ログは DB ではなくアプリログへ** — 「失敗記録を DB に残したい」という
   ニーズは rollback と相性が悪い。当面はアプリログで代替し、本当に必要に
   なったら別セッション・別トランザクションの専用仕組みを用意する
3. **409 Conflict 時も rollback される** — `DuplicateError` を raise する
   直前までに flush していた変更があれば、それも巻き戻る。警告的な用法は
   不可能

### 採用しなかった選択肢

- **Service 内部にトランザクション境界を書く方針** — 各メソッド先頭に
  ボイラープレートが入る。Service が session を直接持つ責務拡大が起きる。
  書き忘れのリスクが構造的に残る
- **デコレータでトランザクション境界を付与する方針** — 自前実装が必要。
  付け忘れのリスクが残る。dependency 方式よりも規律が弱い
- **HTTP と Worker を同じ構造で揃える方針** — 「性質の違うものを揃える」
  という誤り。Worker 側の長時間処理要件と合致しない

### スコープの限定

今のリファクタリングブランチは **HTTP 経路のみ** を対象とする。
Worker 経路の Service は以下の理由で本ブランチのスコープ外：

- 本質的にパイプライン機能の設計と一体なので単独では設計しきれない
- 今巻き込むとブランチの意図がぼやける
- パイプライン実装のタイミングで、その文脈で設計するのが正しい

### CLAUDE.md への反映事項

論点1の実装時に `backend/CLAUDE.md` に以下を追記する：

- **Always do**
  - HTTP Service は session を dependency から受け取るだけとし、
    トランザクション境界を自分で書かない
  - 例外（ドメイン例外含む）は常に rollback 対象とみなす
- **Never do**
  - Repository で commit / refresh を呼んではならない
  - Service メソッド内で部分 commit を実現しようとしてはならない
    （必要なら Service を分割する）

## 論点2: Repository の戻り値と `flush` の扱い — **決定済み**

論点2は次の4つの小論点に分解して議論した：

- **A. `flush` をどこで呼ぶか** — 決定済み
- **B. Repository メソッドの戻り値** — 決定済み
- **C. 生成系メソッドの責務分担（ファクトリの所在・戻り値・命名）** — 決定済み
- **D. `updated_at` の更新をどう保つか** — 決定済み

### 論点2-A: `flush` をどこで呼ぶか — **決定済み (2026-04-11)**

**必要なとき（create 系で ID 採番が必要な場合）だけ `flush` を呼ぶ方針を採用する。**

- update 系・delete 系では `flush` を呼ばない
- 全メソッドで機械的に `flush` するのは過剰
- Service 側で `flush` を気にするのは責務漏れ。Repository 内で完結させる

### 論点2-D: `updated_at` の更新をどう保つか — **決定済み (2026-04-11)**

#### 背景と問題意識

論点1で「Repository は refresh しない」と決定したため、`updated_at` を
DB 側（`server_onupdate`、トリガー等）で更新する方式を採ると、
メモリ上の `source.updated_at` と DB の値が乖離する。flush 後のレスポンスに
古い `updated_at` が載る、という実質的なバグが発生する。

この問題は、論点1の「メモリ上の値が先に変わり、DB はそれを追従する」という
原則と整合しない。`updated_at` もこの順序に乗せるべき。

#### 決定

**メモリ上のオブジェクトを経由するドメイン操作については、
Model のドメインメソッド内で直接 `updated_at` を更新する方針を採用する。**

- ドメインメソッドの中で、状態変更と同じ場所で明示的に時刻を代入する
- 抽象化（`touch()` ヘルパー、`now_utc()` ヘルパー、`before_flush` イベント等）
  は現時点では採用しない
- 書き忘れリスクは受け入れる。代わりに Model メソッドを読めば振る舞いが
  全て見える透明性を得る

#### 抽象化を採用しない理由

- **使用箇所の規模が不明な段階で共通化すると、抽象化のコストが得られる
  利益を上回る可能性がある**。必要になったら切り出す
- `touch()` のような名前は「何をするメソッドか」の意味論を別途文書化する
  必要が生じ、認知コストを増やす
- 直接 `self.updated_at = datetime.now(UTC)` と書く方が、読者にとって
  透明性が高い
- この判断は memory の `feedback_coincidental_duplication` と整合する
  （「たまたま同じコード」は将来同じ理由で変わるかで判断する）

#### 例外（本規約の対象外）

- **DB 側で完結する更新**（トリガー、バッチ、SQL 直接発行）は本規約の対象外
- 本規約は「メモリ上のオブジェクトを経由する更新」に限定する
- たとえば記事の統計集計を SQL で一括更新するような場合は、DB 側で
  updated_at を扱っても良い

#### 採用しなかった選択肢

- **D2: Base クラスに `touch()` ヘルパーを置く** — 使用箇所の規模が不明で
  共通化の価値が測れない。`touch()` の意味論を別途文書化する必要が生じる
- **D3: `before_flush` イベントで自動化** — 「ドメイン上の意味のある更新」
  と「技術的な自動更新」を混同する設計。全ての flush で一律に時刻を書き換えるのは
  粗すぎる。意図のない更新まで `updated_at` が変わってしまう
- **D4: Model の `onupdate=datetime.now` 指定** — メモリと DB の乖離問題を
  解決できない。論点1の refresh 禁止と相性が悪い

### 論点2-B: Repository メソッドの戻り値 — **決定済み (2026-04-11)**

#### 背景

論点1で「Repository は refresh しない」と決めたため、Repository の
ミューテーションメソッドは「呼び出し元が既に持っている Python オブジェクトの
状態を変える」だけになる。Identity Map と参照セマンティクスにより、
インスタンスを返す方式（B1）と None を返す方式（B2）は技術的に等価で、
返されるのは呼び出し元が渡したのと同一の Python オブジェクトである。
したがって純粋に書き味と規約の選択になる。

#### 決定

**Repository のミューテーション系メソッドは `None` を返す方針を採用する。**

- ミューテーション系（`activate`, `deactivate`, `delete` 等）は副作用メソッド
  として正直に `None` を返す
- 呼び出し元は引数で渡したインスタンスをそのまま使い続ける
- 本決定の時点では Repository メソッドを次の3種類に分けて議論していた：
  - **クエリ系**（`get_by_id`, `list_all`, `_get_or_raise`）→ インスタンスを返す
  - **生成系**（`create` / `save`、論点2-C 次第）→ インスタンスを返す（ID 採番のため）
  - **ミューテーション系**（`activate`, `deactivate`, `delete`）→ `None` を返す
- **追記（論点2-C-2 で再検討）**: 上記の生成系の戻り値は論点2-C-2 で
  「インスタンスではなく `None` を返す」に変更された。結果として戻り値の
  型シグネチャは「クエリ系 → インスタンス / それ以外 → None」の **2種類** に
  簡素化される

#### 採用理由

- **「同一オブジェクトを返す」のは読者にとって誤解を招く**: B1 で `updated = ...`
  と書くと「別物かもしれない」という錯覚を生む。論点1で refresh を禁じた以上、
  `updated` という命名自体が嘘になる
- **論点1の構造的強制スタンスと整合**: 「戻り値を別変数に受けて、つい古い変数を
  捨てる」というミスを構造的に防げる（None を変数に受けても何も使えない）
- **Pythonic な CQS（Command-Query Separation）**: `list.sort()` が None を返す
  のと同じ思想。Command 系は副作用に専念し、値を返さない

#### 採用しなかった選択肢

- **B1: インスタンスをそのまま返す** — fluent な書き味は得られるが、Vector の
  Service 層は fluent パターンを使っておらず利益がない。同一オブジェクトを
  別名で受ける構文が「refresh されたかもしれない」という誤読を誘う
- **メソッド単位で気分で使い分ける** — 一貫性が崩れる

#### Rich Domain Model 移行後の射程

論点3（Rich Model 化）が進むと、`activate`/`deactivate` のような状態遷移は
Repository から Model に移る。その世界では本規約は **`delete` などの
「Model に移せない session 操作」にだけ適用される**。それでも以下の理由で
今この規約を決めておく価値がある：

- Rich Model 移行は段階的なので、過渡期に「mutation を持つ Repository メソッド」
  がしばらく存在する
- `delete` は Rich Model 化後も Repository に残る
  （エンティティ自身が「自分を削除する」のは不自然）
- 将来 Rich Model に乗らない例外的なケースが出てきたときの規範になる

#### 補足: 削除系メソッドの「存在しなかった」検知 — **決定済み (2026-04-12)**

##### 背景

Vector の現状では、削除系 Repository メソッドが rowcount を返し、Service が
それを見て「対象が存在しなかった」を判定して `NotFoundError` を raise する
パターンが存在する。

例（[backend/app/services/watchlist.py:44-47](backend/app/services/watchlist.py#L44-L47)）:

```python
deleted = await self.repo.unwatch(user_id, article_id)
if deleted == 0:
    raise NotFoundError("Watchlist item not found")
```

論点2-B で「ミューテーション系は None を返す」と決めたので、この情報経路は
規約と衝突する。どう扱うかを決める必要がある。

##### 決定

**Service 側で「事前 exists チェック + 副作用のみの delete」パターンに
書き換える方針を採用する（選択肢 b）。**

- Repository の削除系メソッドは `None` を返す（規約を例外なく貫く）
- Service が `await repo.exists_*(...)` 等で事前確認を行い、存在しない場合は
  `NotFoundError` を raise
- 存在する場合のみ `await repo.delete(...)` を呼ぶ
- ドメイン判断（NotFound）と副作用（delete）が Service 層で分離される

##### 採用理由

- **論点2-B / 2-C-2 で確立した規約を例外なく貫ける**（削除系だけ rowcount を
  返す例外を入れない）
- **Service 側で「ドメイン判断」と「永続化操作」が分離されて読みやすい**
- **既存の他の query+mutation 系（`activate`/`deactivate` など）と書き味が揃う**
  — それらも `_get_or_raise → mutation` の順で書かれているため
- **論点1 の dependency-managed transaction が同一トランザクション内** なので、
  exists チェックと delete の間に並行リクエストが入って TOCTOU が起きる確率は
  実質ゼロに近い

##### 採用しなかった選択肢

- **(a) Service が rowcount を受け取って判定する** — 論点2-B の規約に例外節が
  生まれる。「mutation 系は None」のシンプルさが崩れる
- **(c) 削除系は bool / rowcount を返してよい例外節を入れる** — Yk さんの
  「一貫性が崩れるリスクは重大」スタンスと整合しない

##### watchlist の現状コードの扱い

`watchlist.unwatch` の rowcount 経由パターンは、論点3-X（watchlist 再
モデリング）の本格修正前提なので、PR 1 段階では (b) パターンに **暫定的に
書き換える** だけで良い。論点3-X で User Aggregate 視点から改めて見直す。

### 論点2-C: 生成系メソッドの責務分担 — **決定済み**

論点2-C は当初「`create` と `save` を分けるか統合するか」という設定で開始したが、
議論の過程で以下の3つのサブ論点に分解されることが判明した：

- **C-1: ファクトリの所在** — Model のインスタンス化を Service / Repository の
  どちらが担うか — 決定済み
- **C-2: 生成系メソッドの戻り値** — 生成系は論点2-B と揃えて None を返すか、
  例外的にインスタンスを返すか — 決定済み
- **C-3: メソッド名** — `create` / `save` / `add` / `register` のどれを規約にするか
  — 決定済み（命名のみ。意味論は論点3 に持ち越し）

#### 論点2-C-1: ファクトリの所在 — **決定済み (2026-04-11)**

**Service が schemas → Model の変換を行い、完成したインスタンスを Repository に
渡す方針を採用する。例外は認めない。**

- Repository は schemas を import しない（依存方向の原則を守る）
- Repository は既に組み立てられた Model を受け取り、永続化のみを行う
- Rich Model 化（論点3）に進んだ場合も、Model のクラスメソッド
  （例: `Model.create_from(...)`）やコンストラクタを呼ぶのは Service 側
- 結果として Service 層が「ドメイン入口のマッピング責務」を一貫して持つ

##### 採用理由

- **schemas → Model のマッピングはドメイン入口の責務**。これは Service の
  仕事で、Repository に持たせると依存方向が逆転する
  （`feedback_layer_architecture` と整合）
- **Rich Model 化と相性が良い**。論点3 で Rich Model に向かうと、Model の
  インスタンス化は Service から行う構造に自然に乗る
- **テスタビリティ**: Service で Model を組み立てれば、Repository をモックに
  したテストで「どんなインスタンスが渡されたか」を素直にアサートできる

##### 採用しなかった選択肢

- **Repository がファクトリを担う** — schemas の知識を Repository に
  持ち込むことになり、レイヤー原則に反する
- **「結合テーブル等は Repository が組み立てる」という例外運用** — 一貫性の
  例外は増殖しやすく、Yk さんのスタンス（一貫性を構造的に守る）と整合しない

##### watchlist の現状について

Vector には現在、`watchlist` Repository が primitives（`user_id`, `article_id`）
を受け取って内部で `WatchlistEntry(...)` を組み立てる実装が存在する。これは
本規約の観点では「例外的な形」になるが、**議論の過程でこの実装の根本原因は
「`WatchlistEntry` を独立エンティティとして設計したことが中途半端な状態を生んで
いる」ことであると特定された**。

本規約に例外節を設けて watchlist を追認するのではなく、watchlist のモデリング
そのものを後続ブランチで見直す方針を採る。現時点では watchlist は本規約の
射程外として扱い、`refactor/news-source-activation` ブランチおよびその直後の
Tx 境界リファクタリングでも watchlist には触らない。

詳細は `specs/backlog/watchlist-remodeling.md` を参照。

#### 論点2-C-2: 生成系メソッドの戻り値 — **決定済み (2026-04-11)**

##### 背景

論点2-B で「Repository のミューテーション系は `None` を返す」と決定したが、
そのときは生成系の戻り値を「次の論点（C）に持ち越す」として未決にしていた。
ここで生成系を本決着させる。

技術的事実：論点2-C-1 で「Service が Model を組み立てる」と決まったので、
生成系のコードは次の流れになる：

- Service 側で `source = NewsSource(...)` を組み立てる
- Repository に `source` を渡す
- Repository は `session.add(source)` + `session.flush()`
  （論点2-A に従い ID 採番のために flush）
- flush 後、Identity Map と参照セマンティクスにより、呼び出し元の `source`
  変数の `source.id` が自動的に埋まる

つまり「インスタンスを返す（C-2a）」と「None を返す（C-2b）」は技術的に等価で、
返ってくるのは呼び出し元が渡したのと同一の Python オブジェクトである。
論点2-B の B1/B2 と同じ構図。

##### 決定

**生成系メソッドも `None` を返す方針を採用する（C-2b）。**

結果として、Repository メソッドの戻り値の型シグネチャは次の **2分類** に
簡素化される：

| 種類 | 戻り値 | 用途 |
|---|---|---|
| **クエリ系**（`get_by_id`, `list_all`, `_get_or_raise` 等） | インスタンス | データ取得 |
| **それ以外**（生成系・ミューテーション系・削除系） | `None` | 副作用メソッド |

これは純粋な CQS（Command-Query Separation）そのものであり、
「Command は何も返さない、Query はデータを返す」という古典原則と
Repository の型シグネチャが完全に一致する。

##### 採用理由

- **論点2-B と同じ論理がそのまま適用される**: 「同一オブジェクトを別名で
  受ける」のは読者にとって誤解を招く。論点1 で refresh を禁じた以上、
  生成系で `created = await repo.save(source)` と書いても `created` と
  `source` は同一の Python オブジェクトであり、別名で受けると誤読を生む
- **戻り値規約が「2種類」に統合される**: 論点2-B 時点では3分類だったが、
  本決定で2分類に簡素化される。読者は型シグネチャだけで Command/Query を
  判別でき、メソッド名のニュアンスを覚える必要がない
- **Rich Model 化後も自然に機能する**: 論点3 で Rich Model に進むと、
  Service は `source = NewsSource.create_from(body)` のように Repository
  呼び出し前にインスタンスを変数に束縛する流れが自然になる。生成系の戻り値
  は使われなくなるので、None で十分

##### 採用しなかった選択肢

- **C-2a: インスタンスを返す** — 論点2-B の B1 と同じ誤読リスクを生成系で
  許容することになる。Rich Model 化後に戻り値が実質的に死ぬ。「3分類」
  になり規約が複雑化する

#### 論点2-C-3: メソッド名 — **決定済み (2026-04-11)**

##### 決定

**Repository の生成系メソッドは `save` という名前を採用する。**

ただし「`save` が新規エンティティ専用か、UPSERT セマンティクス（新規も
更新も両方扱う）か」という意味論の選択は **論点3 に持ち越す**。論点2-C-3
で確定するのは命名のみ。

##### 命名選択の採用理由

- **直感的に通じる普遍語彙**: DDD 文献を知らない読者にも一読で意味が通る
- **`session.add` との混同を避けられる**: `add` は SQLAlchemy の用語と被る
- **Vector の他の Repository メソッド名と語感が揃う**: `delete` などと同じ
  「動詞 + 対象」のパターンに馴染む
- **`feedback_repository_naming` の「ドメイン語彙」原則と整合**:
  「保存する」はドメイン語彙として通用する

##### 命名以外で採用しなかった選択肢

- **`add`**: DDD 原書の語彙だが、`session.add` との混同リスクがあり、
  Vector の他のメソッド名と語感が合わない
- **`create`**: 戻り値が None なのに「create」だと「何かを作って返す」感が
  残り、論点2-C-2 の決定と緊張する
- **`persist`**: Hibernate 由来。Pythonic でなく、日常語彙としても弱い
- **`register`**: 「登録」のニュアンスが強すぎ、技術的な「永続化」より広い
  意味を持ち込んでしまう

##### `save` の意味論を論点3 に持ち越す理由

`save` には ORM 慣例として「UPSERT セマンティクス」（新規も既存も両方扱う）
の含意がある。Vector でこれをどう扱うかには2つの立場がある：

- **立場 A（UoW 依存）**: `save` は新規エンティティ専用とする。既存
  エンティティの状態変更は Model のドメインメソッドで mutate するだけで、
  dependency-managed transaction（論点1）の commit 時に UoW が自動で
  flush + UPDATE を発行する。Service は既存エンティティに対して `save` を
  呼ばない
- **立場 B（明示 save）**: `save` は新規も既存も両方扱う UPSERT として
  振る舞う。Service は状態変更後に常に `await repo.save(source)` を
  明示的に呼ぶ。Django/Rails の `model.save()` と同じセマンティクス

両者の核心は「Rich Model 化された後の Service コードで、既存エンティティの
mutate 後に明示的な save 呼び出しが必要か」という問いに帰着する。これは
論点3（Rich Model の適用範囲）と地続きの問題なので、論点2-C-3 では命名のみ
を確定し、意味論は論点3 で正面から議論する。

詳細は `論点3` セクション内の「サブ論点: save の意味論（立場 A vs 立場 B）」
を参照。

## 論点3: Rich Domain Model の適用範囲 — **建て付けを2軸に分割 (2026-04-12)**

### 建て付けの再構成

論点3 は当初「Rich Model 化の単位（エンティティ単位 / Aggregate 境界）」と
いう抽象論として設定したが、議論の過程で **Vector の現状を実コードで精査
した結果、性質の異なる2軸が存在する** ことが判明した。一般化された「子要素
ポリシー」を先に決めようとすると、必ず例外が出る構造になっている。

そこで論点3 を以下の2軸に分割する：

#### Vector に存在するエンティティの分類

実コードを精査した結果、Vector のエンティティは次の3グループに分けられる：

| 種類 | 例 | Rich 化との関係 |
|---|---|---|
| **状態機械を持つ自律エンティティ** | `NewsSource`, `Keyword`, 将来 `ArticleAnalysis` | Rich Model 化の本命 |
| **独立マスタデータ** | `Category` | 状態遷移なし。Rich 化の必要薄い |
| **結合テーブル（関係の記録）** | `WatchlistEntry`, `ArticleKeyword` | 中途半端な独立エンティティ問題が起きる場所 |

`Keyword` は `PROVISIONAL → OFFICIAL → BLACKLISTED` の状態遷移と
`approved_at` を持つため、`NewsSource` と同型の自律エンティティであり、
結合テーブル `ArticleKeyword` とは別物である。これらを混同しない。

さらに結合テーブル軸の中でも書き込み経路が分岐する：

- `WatchlistEntry`: HTTP 経路、ユーザー操作で個別に追加・削除
- `ArticleKeyword`: Worker 経路、Analysis 作成時に一括生成して以後固定

論点1 で確立した「HTTP と Worker は性質が違うから揃えない」原則に従い、
これらは **同じ方針で扱わない**。

### 論点3-Y: 状態機械エンティティの Rich Model 化 — **先行して着手**

対象: `NewsSource`, `Keyword`, 将来的に `ArticleAnalysis` 等

「状態遷移を持つエンティティの状態変更を Model 側に移す」という単一テーマ
で進める。`save` の意味論（立場 A vs 立場 B）はこの軸で決着する。

#### 進め方

1. `news_source` を最初の Rich 化対象として実装に降りる
2. その実装で `save` の意味論を **議論ではなく実物で** 決着させる
3. CLAUDE.md への規約追記もこの段階で完了
4. その経験を持って `Keyword` 等に展開

#### なぜ news_source 先か

- 残っている問いが「状態遷移の Model 化と save 意味論」だけに絞られている
  純粋系
- 論点1・論点2 の決定をすべて一度に検証できる
- `news_source` は現在のブランチの主題そのもので、コードが目の前にある
- 一例を実装に降ろせば、書き味・薄さ・違和感の有無が議論ではなく実物で
  わかる

#### サブ論点「`save` の意味論」

論点2-C-3 で `save` という命名は確定したが、その意味論（新規専用か UPSERT か）
は論点3 に持ち越された。この問いは「Rich Model 化された後の Service コードで、
既存エンティティの mutate 後に明示的な save 呼び出しが必要か」と等価であり、
論点3-Y の実装によって決着する。詳細な立場 A / B の整理は本セクション末尾を
参照。

### 論点3-X: 結合テーブル（関係の記録）の置き場所 — **論点3-Y の後に着手**

対象: `WatchlistEntry`, `ArticleKeyword`

この2つは同型に見えて、書き込み経路の性質が根本的に違う。**統一を目標に
しない**。それぞれを独立に解く。

#### WatchlistEntry（HTTP 経路）

- 詳細は `specs/backlog/watchlist-remodeling.md` で深堀り
- 論点3-Y（news_source の Rich 化）が完了した後に着手する
- 理由: Rich Model 化の道具（メソッドの置き方、`save` の呼び方、規約）が
  確立済みの状態で取り組めば、watchlist のモデリング修正だけに集中できる
- watchlist は機能的バグではなくモデリングの歪みなので、論点3-Y 完了まで
  現状放置で害は出ない

#### ArticleKeyword（Worker 経路）

- 既存の FK 張替えタスク（`project_keyword_fk_migration` memory）の中で
  別個に判断する
- Worker 経路設計の文脈で扱うのが筋
- 本論点3 では決着させない

### 論点3-Y を先にやる理由（建て付けの根拠）

watchlist には2つの独立した問題が同時に乗っている：

1. モデリング問題: WatchlistEntry が中途半端な独立エンティティとして設計
   されている
2. Rich 化問題: 判断ロジック（重複禁止、対象が分析済みか）の置き場所

watchlist 先だとこの2つが同じ議論で混ざる。Rich 化の道具がまだ確立して
いない状態で「User Aggregate 的に user.watch() を作るべきか」を議論する
ことになり、判断軸の一貫性が崩れる。

論点3-Y を先にやれば、Rich 化の道具を確立してから論点3-X（watchlist）に
降りられるので、watchlist 議論はモデリング問題だけに集中できる。

### `save` の意味論（立場 A vs 立場 B）の整理

論点2-C-3 で `save` という命名は確定したが、その意味論（新規専用か UPSERT か）
は論点3 に持ち越された。この問いは「Rich Model 化された後の Service コードで、
既存エンティティの mutate 後に明示的な save 呼び出しが必要か」と等価であり、
論点3 と切り離せない。

本セクションは `save` の意味論に関する立場 A / B の比較資料。論点3-Y の
実装段階で正面から扱う。

#### 立場 A: UoW 依存

- `save` は **新規エンティティ専用**
- 既存エンティティの状態変更は Model のドメインメソッドで mutate するだけ
- dependency-managed transaction（論点1）の commit 時に SQLAlchemy の
  Unit of Work が dirty なエンティティを自動で flush + UPDATE
- Service は既存エンティティに対して `save` を呼ばない
- 結果として Service コードは非対称になるが、その非対称性が
  「`save` が呼ばれている = 新規エンティティ」というシグナルとして機能する

##### 立場 A のメリット
- SQLAlchemy のイディオムに素直（既存エンティティへの `session.add` は
  公式に「冗長」とされている）
- `save` が呼ばれていることが情報を持つ（呼ばれている = 新規）
- Rich Model のクリーンさを汚さない（Model 側で完結する状態変更を、
  Service 側で「保存命令」として二重に表現しない）
- Yk さんの構造的強制スタンスと整合：「呼んでも呼ばなくても結果が同じ」
  なメソッドはサイレント成功を許容するが、立場 A はそれを避ける

##### 立場 A のデメリット
- Django/Rails 的な「明示的に save する」感覚に慣れた読者には、
  状態変更後に save が呼ばれないコードが不自然に見える可能性
- 「dependency commit が UoW を回す」という仕組みを読者が理解している
  必要がある

#### 立場 B: 明示 save（UPSERT セマンティクス）

- `save` は **新規も既存も両方扱う UPSERT** として振る舞う
- Service は状態変更後に常に `await repo.save(source)` を明示的に呼ぶ
- Django/Rails の `model.save()` と同じセマンティクス
- Service コードは新規・更新問わず対称的になる

##### 立場 B のメリット
- Service コードが対称的で、「永続化の意図」が常に明示される
- Django/Rails 出身者にとって馴染みのあるパターン
- 「mutate したら save する」という単純な規約

##### 立場 B のデメリット
- 既存エンティティへの `save` 呼び出しは技術的に no-op に近い
  （SQLAlchemy が既にトラッキング済みなので、`session.add` は冗長で、
  early flush するかどうかの違いしか生まない）
- 「呼んでも呼ばなくても結果が同じ」な呼び出しがコードに散在することで、
  読者が「これは本当に必要な呼び出しか？」と判断に迷う
- Rich Model の `source.activate()` が「自分で完結する」と言っているのに、
  外側で `repo.save(source)` を呼ぶ二重表現になる

#### 仮の方針

**現時点では立場 A を推奨候補として記録する。** 論点3-Y で `news_source`
を Rich 化する実装の中で、実コードを書きながら正面から検証する。

立場 A を仮推奨とする理由：
- 論点1（dependency-managed UoW）の世界観に最も素直に乗れる
- Yk さんの「構造的強制」「サイレント成功を避ける」スタンスと整合する
- SQLAlchemy 公式イディオムと一致する

立場 A を実装で検証する観点：
- `news_source` の `update_*`, `activate`, `deactivate` を Model 側に移した
  あとの Service コードが、立場 A 前提で本当に自然に書けるか
- `dependency commit が UoW を回す` という暗黙仕様を読者が意識せずに
  読めるか
- 既存エンティティに save を呼ばないコードが「書き忘れ」と誤読されないか
- もし違和感が出るなら立場 B に切り替える

## 論点4: `_get_or_raise` の重複問題 — **未決**

（独立した別論点として扱う）

## 移行計画（2026-04-12 更新）

論点1・論点2 が決着したので、論点3-Y の実装を進めながら段階的に PR を
切る。各 PR は独立してマージ可能な単位に区切る。

### フェーズ1: 基盤整備（論点1・論点2 の実装）

1. **spec 昇格** — 本ドキュメントを `specs/` 直下に昇格
2. **PR 1: `get_session` dependency への境界導入 + Repository から commit を剥がす**
   - `async with session.begin():` を dependency 層に集約
   - 全 Repository から `commit` / `refresh` 呼び出しを除去
   - Repository の戻り値規約（クエリ系 → インスタンス / それ以外 → None）に
     合わせて既存メソッドのシグネチャを調整
   - この PR は Rich 化を含まない。純粋な構造リファクタリング

### フェーズ2: 論点3-Y（状態機械エンティティの Rich 化）

3. **PR 2: `news_source` を Rich Model 化**
   - `activate` / `deactivate` などの状態遷移を Model のドメインメソッドに
     移す
   - `updated_at` の更新を Model メソッド内で直接代入
   - Service の `activate_source` / `deactivate_source` が薄くなる
   - **この PR で `save` の意味論（立場 A vs B）を実コードで決着させる**
   - 決着後、規約を `backend/CLAUDE.md` に追記する PR をペアで出す
4. **PR 3: CLAUDE.md への規約追記**
   - 論点1・論点2 の規約 + 論点3-Y で確定した `save` 意味論
   - Rich Model のドメインメソッド内で `updated_at` を直接代入する規則
5. **PR 4 以降: `Keyword` の状態遷移を Rich 化**
   - `PROVISIONAL → OFFICIAL → BLACKLISTED` の遷移を Model に移す
   - `news_source` で確立した規約をそのまま再利用
6. **PR 5 以降: `ArticleAnalysis` 等への展開**
   - 状態遷移を持つ他エンティティに段階的に適用

### フェーズ3: 論点3-X（結合テーブル）

7. **WatchlistEntry の再モデリング**
   - フェーズ2 完了後に着手
   - `specs/backlog/watchlist-remodeling.md` の内容を具体化して spec 昇格
   - 論点3-Y で確立した規約を前提に、モデリング修正だけに集中
8. **ArticleKeyword の FK 張替え**
   - `project_keyword_fk_migration` memory のタスクで別軸進行
   - Worker 経路設計の文脈で扱う
   - 本移行計画とは独立して進められる

### スコープ外

- **Worker 経路の transaction 設計** — パイプライン実装と一体で行う
  別タスク。本移行計画には含めない

## 参考

- 議論の発端: `refactor/news-source-activation` ブランチ
- 関連 memory: `feedback_layer_architecture`（3層分離、ドメイン例外で
  Service → Router 接続）
