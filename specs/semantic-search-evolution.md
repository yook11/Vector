# セマンティック検索の進化ロードマップ

## 現状の位置づけ

現在のセマンティック検索は **トピック類似度フィルタ** であり、高機能な LIKE 検索に相当する。

- **embedding 対象**: `original_title + original_content`（記事の生テキスト）
- **検索方式**: クエリを `RETRIEVAL_QUERY` でベクトル化し、`cosine_distance < 0.8` でフィルタ
- **ソート**: embedding の距離ではなく `published_at` 順（フィルタとソートの役割を分離）
- **限界**: 表層的なトピック類似度しか捉えられない。因果関係や間接的な影響関係は検出不可

### 因果関係が捕捉できない例

「米国が半導体輸出規制を強化」→ 以下は因果的につながるが embedding 空間での距離は異なる:

| 記事 | embedding 距離 | 因果関係 |
|---|---|---|
| NVIDIA の決算見通し | 近い（共起頻度が高い） | 直接影響 |
| TSMC の設備投資計画 | やや近い | 直接影響 |
| 日本の半導体政策 | 中程度 | 間接影響 |
| 自動車生産遅延 | 遠い | 2ホップの影響 |
| 保険業界の業績 | 非常に遠い | 3ホップの影響 |

問題の本質は「近いか遠いか」ではなく **推論のホップ数**。embedding は 0 ホップ（同一トピック）に強く、ホップが増えるほど捕捉率が下がる。

## 段階的改善ロードマップ

### Phase 0: 現状（済）

記事本文の embedding + cosine distance フィルタ。

### Phase 1: 分析結果を embedding に含める（低コスト・中改善）

**変更点**: `_build_embed_text()` に `reasoning` フィールドを追加。

```python
# Before
def _build_embed_text(article: NewsArticle) -> str:
    body = article.original_content or article.original_description or ""
    return f"{article.original_title}\n{body}"

# After
def _build_embed_text(article: NewsArticle, analysis: ArticleAnalysis | None = None) -> str:
    body = article.original_content or article.original_description or ""
    text = f"{article.original_title}\n{body}"
    if analysis and analysis.reasoning:
        text += f"\n{analysis.reasoning}"
    return text
```

**根拠**: Gemini の分析結果 `reasoning` には「この記事は〇〇業界に影響する」「〇〇政策の延長線上にある」等の因果的記述が含まれている。これを embedding 対象に加えることで、因果的に関連する記事同士の距離が縮まる。

**コスト**: embedding の再生成が必要（バッチで実行可能）。API コスト以外の追加コストなし。

**期待効果**: 1ホップの因果関係の捕捉率が向上。2ホップ以上は依然として弱い。

### Phase 2: エンティティグラフ（中コスト・中〜高改善）

記事間の関係を企業・政策・技術などのエンティティを介したグラフ構造で保持する。

- 記事分析時に関連エンティティを抽出（Gemini で構造化出力）
- エンティティ → 記事の逆引きインデックスを構築
- 検索時: クエリに関連するエンティティを特定 → そのエンティティに紐づく記事を候補に含める

**コスト**: 新テーブル（entities, article_entities）、分析パイプラインの拡張。

**期待効果**: 2ホップまでの因果関係を構造的に捕捉可能。

### Phase 3: LLM re-ranking（高コスト・高改善）

embedding + エンティティグラフで候補を取得し、LLM で因果関係の妥当性を判定して re-rank する。

- Phase 1-2 で候補を 20-50 件に絞る（コスト制御）
- LLM に「この記事はクエリの出来事に影響を受けるか？」を判定させる
- 因果関係のスコアで re-rank

**コスト**: 検索リクエストごとに LLM 呼び出し（候補数に比例）。

**期待効果**: 非自明な因果関係（3ホップ以上）も推論可能。

### Phase 4: 全候補 LLM 判定（最高コスト・最高精度）

embedding フィルタなしで LLM に全候補を判定させる。現実的には Phase 3 で十分であり、Phase 4 は研究的アプローチ。

## 判断基準

Phase を進めるかどうかは以下で判断する:

1. **記事数の規模** — 数百件なら Phase 1 で十分、数万件なら Phase 2 以降が必要
2. **ユーザーの検索意図** — 「トピック検索」が主なら Phase 1、「影響分析」が主なら Phase 2+
3. **コスト許容度** — Phase 3 以降は検索あたりの LLM コストが発生

## 現在のスコープとの関係

この文書は将来構想の記録であり、現在のスコープ（SortBy 設計 + クエリ embedding キャッシュ）には含めない。現状のセマンティック検索は「フィルタの一種」として正しく位置づけ、過大評価しない。
