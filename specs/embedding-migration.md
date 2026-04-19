# Embedding モデル移行: Gemini → Ruri v3-310m

> ステータス: 設計確定（実装待ち）

## 意思決定

埋め込みモデルを `gemini-embedding-001` から `cl-nagoya/ruri-v3-310m` に移行する。
セマンティック検索は現在動いていないため、段階移行ではなく一括で切り替える。

## 選定理由

3 つの軸で判断した。

**性能**: JMTEB 平均 77.24 は、商用利用可能な日本語 embedding モデルとして事実上の SOTA。
315M パラメータで sarashina-1.22B（75.50）や PLaMo-1B（76.10）を上回るサイズ効率の良さは、
solo developer 運用として決定的。

**ライセンス**: Apache 2.0。sarashina 系・PLaMo 系・Jina v4 は非商用ライセンスのため除外。
ポートフォリオとして公開する前提で、ライセンスの懸念がないことは必須条件。

**pgvector 整合性**: 出力 768 次元が現行の `Vector(768)` と完全一致する。
スキーマの次元数変更が不要であり、マイグレーションの範囲を最小化できる。

### 除外したモデル

- **sarashina-embedding-v1-1b** — 非商用ライセンス
- **PLaMo-Embedding-1B** — 非商用ライセンス
- **Jina Embeddings v4** — 非商用ライセンス
- **Qwen3-Embedding-0.6B** — 多言語対応が強みだが、Vector は英語原文を embedding しない方針のため不要
- **ruri-v3-130m** — 310m との JMTEB 差（76.55 vs 77.24）は小さいが、768 次元一致の副次メリットが 310m にしかない

## 移行で変わること

### 推論インフラ: API → ローカル

Gemini API への HTTP 呼び出しを、Docker Compose 内の TEI（Text Embeddings Inference）コンテナへのローカル呼び出しに置き換える。
TEI は HuggingFace が提供する推論サーバーで、ModernBERT を公式サポートしている。
CPU 版の Docker イメージで十分実用的に動作する（ModernBERT のメモリ効率が高い）。

これにより以下が変わる:
- Gemini Embedding の RPD 1500 制約が消える。リクエスト数に上限がなくなる
- API 課金がゼロになる。embedding は件数が多く、課金インパクトが最も大きい処理だった
- 検索クエリの embedding レイテンシが改善する（ネットワーク往復がなくなる）

AI 分析（翻訳・分類）は引き続き Gemini API を使う。
件数が少なくコスト影響が小さいため、ローカル化の優先度は低い。

### 埋め込み対象テキスト: 英語 → 日本語

現行は英語原文（`original_title + original_content`）を埋め込んでいるが、
ruri-v3 は日本語特化モデルのため、翻訳後の日本語テキスト（`translated_title + summary`）を埋め込む。

Vector のセマンティック検索は日本語 UI から日本語クエリで行う。
日本語テキストを日本語特化モデルで埋め込むのが最も自然な組み合わせ。

パイプライン上の依存関係は変わらない。
embedding は analyze_article の後に実行されるため、翻訳済みテキストは常に利用可能。
prompt-split 実装後は `title_ja + summary_ja`（事実ベース要約）になり、embedding 品質がさらに向上する。

### プレフィックス方式: API パラメータ → テキスト先頭

Gemini は API パラメータとして `task_type`（`RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`）を指定し、
モデル側が用途に応じた表現を学習する仕組みだった。

ruri-v3 はテキスト先頭にプレフィックスを付与する方式（1+3 プレフィックス）を採用している。

| 用途 | プレフィックス |
|---|---|
| 文書のインデックス | `検索文書: ` |
| 検索クエリ | `検索クエリ: ` |
| クラスタリング | `トピック: ` |
| 汎用 | なし |

プレフィックスはモデル固有の知識であるため、Embedder の内部に閉じる。
`BaseEmbedder` にプレフィックスの ClassVar を追加し、`embed_document` / `embed_query` が
内部でプレフィックスを付与する設計にする。呼び出し側はプレフィックスの存在を意識しない。

これに伴い `_call_api` のシグネチャから `task_type` パラメータを削除する。
Gemini 固有の概念であり、プレフィックス方式では不要になる。

### DB 型: Vector → HALFVEC

embedding の格納型を `Vector(768)`（float32）から `HALFVEC(768)`（float16）に変更する。

halfvec は pgvector 0.7.0 で導入された半精度浮動小数点型。
768 次元で 1 行あたり約 3,080 bytes → 1,544 bytes に半減する。
実測で recall にほぼ影響がないことが報告されている。

Docker イメージ `pgvector/pgvector:pg16` の最新ビルドは pgvector 0.8.x を含むため、
halfvec は利用可能。Python pgvector 0.4.2 も `HALFVEC` 型をサポートしている。

HNSW インデックスの ops class は `vector_cosine_ops` → `halfvec_cosine_ops` に変更する。

初回から halfvec で始めれば再エンコードのコストを回避できる。
後から変更するとなると全ベクトルの再生成が必要になるため、最初から採用するのが合理的。

### レートリミッターの扱い

ローカル推論のため `RPM` / `RPD` は `None`（制限なし）に設定する。
Task 層の `_build_limiters()` は `None` に対して limiter を生成しない既存ロジックのため、
コード変更なしでそのまま動作する。

### 設定の分離

embedding の provider を `ai_provider`（AI 分析用）から独立させる。
TEI エンドポイントの URL を設定値として持ち、ファクトリは Ruri 固定で返す。
Gemini Embedder は削除する。

### 検索クエリ embedding キャッシュ

Redis キャッシュのキープレフィックスにモデル名を含める。
モデル移行時に古いキャッシュが自動的に無効化される。
TEI がローカルでもキャッシュは有用（TEI へのリクエスト自体を削減する）。

## 変更しないもの

- **SemanticSearchRepository** — embedding ベクトルを受け取るだけで、モデルを知らない
- **AI 分析パイプライン** — embedding と独立した関心事
- **フロントエンド** — 検索 API のインターフェースは不変
- **`semantic_search_max_distance`** — 閾値はモデル移行後にチューニングする。モデルごとにコサイン距離の分布が異なるため

## prompt-split との関係

prompt-split の Stage 1 出力が `title_ja + summary_ja` に変わる。
embedding の入力テキストもこれに追従するが、実装の依存関係はない。
どちらが先でも動作する。現行の `translated_title + summary` でも十分機能する。

## 補足: ModernBERT-Ja の特性

ruri-v3 のベースモデル ModernBERT-Ja は以下の特性を持つ:
- SentencePiece トークナイザのみで完結し、MeCab 等の外部形態素解析器が不要
- 最大系列長 8,192 トークン（Gemini Embedding の 2,048 より長い）
- FlashAttention 対応で推論効率が高い
- Docker イメージの依存関係が軽量に保てる
