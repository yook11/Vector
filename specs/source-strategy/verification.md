# リサーチ検証結果サマリ

外部リサーチ文書2本 ([prompt-split/README.md](../prompt-split/README.md)) の主張を4軸で並行検証した結果。

## 1. フルテキストRSS検証

レポートのフルテキスト主張は **53%が不正確** (15件中8件が実際はスニペット)。

### 確認済みフルテキスト (6/15)

| ソース | フィードURL | 配信形式 | 備考 |
|--------|-----------|---------|------|
| VentureBeat | `venturebeat.com/feed/` | description内 ~8,500字 | |
| Engadget | `engadget.com/rss.xml` | description内 ~1,800字 | Yahoo ToS留意 |
| Krebs on Security | `krebsonsecurity.com/feed/` | content:encoded | |
| Schneier on Security | `schneier.com/feed/atom/` | Atom content | |
| Microsoft Research | `microsoft.com/en-us/research/feed/` | content:encoded ~47,000字 | |
| Spaceflight Now | `spaceflightnow.com/feed/` | content:encoded ~3,500字 | |

### 不正確 (フルテキスト主張だが実際はスニペット)

| ソース | 主張 | 実際 |
|--------|------|------|
| TechCrunch | Full | Snippet ~355字、`[...]`で切断 |
| Quanta Magazine | Full (CC BY-ND) | Snippet ~280字。**CCライセンスも誤り** (通常著作権) |
| The Next Platform | Full | **空のdescription** (ヘッドラインのみ) |
| The Record | Full | Snippet ~180字 |
| CleanTechnica | Full | Snippet、`[continued]`付き |
| Electrek | Full | Snippet ~350字 |
| Phoronix | Full | Snippet (フォーラムでフルテキスト要望あり) |
| SemiWiki | Full | Snippet (Read More付き) |

### 逆方向の誤り

| ソース | 主張 | 実際 |
|--------|------|------|
| **IEEE Spectrum** | Snippet (旧`/rss/fulltext`は消滅) | **Full** (description内で最大~15,800字) |

### 未確認

| ソース | 理由 |
|--------|------|
| Semiconductor Engineering | Cloudflare 403 Forbidden |

## 2. 日本語ソース検証

| 主張 | 判定 | 詳細 |
|------|------|------|
| PR TIMES 企業規約 第6条 で無条件再配信可 | **過大評価** | ライセンス自体は存在するがメディア登録・審査が必須。有償目的は明示除外 |
| ITmedia RSS利用条件 抜粋転載禁止 | **正確** | 2020年条件で改変・翻訳・翻案すべて禁止。商用利用は個別相談 |
| ITmedia フィードURL (5件) | **全件正確** | 全5フィードがアクティブ |
| Impress Watch URL (rssad.jp) | **不正確** | `rssad.jp`ドメインは**接続拒否**。正: `*.watch.impress.co.jp/data/rss/1.0/*/feed.rdf` |
| Nikkei xTECH RSS | **正確** | アクティブ、フリー/ペイウォール混在 |
| JAXA press RSS | **正確** | `jaxa.jp/rss/press_j.rdf` アクティブ |
| RIKEN RSS | **部分的** | RSS存在するがURL構造が異なる。転載に正式許可申請必要 |
| JPCERT/CC RSS | **正確** | `jpcert.or.jp/rss/jpcert.rdf` アクティブ |
| NHK RSS | **正確** (間接証拠) | |
| Kyodo "Japan Wire" リブランディング | **正確** | 2025年7月1日確認 |

## 3. 法的・ライセンス検証

| 主張 | 判定 | 詳細 |
|------|------|------|
| Quanta Magazine CC BY-ND | **不正確** | CCライセンス不使用。従来型著作権、再利用に事前書面許可必要 |
| 米連邦コンテンツ = パブリックドメイン | **過度に単純化** | NASA/NIH職員の著作物は対象。DOE国立研究所はcontractor運営で§105対象外の可能性 |
| NVIDIA Developer Blog ToS 商用禁止 | **正確** | 個人・非商用利用に制限 |
| EurekAlert ToS | **部分的** | 全文複製禁止は正確。帰属+リンクバックだけでは不十分、事前書面許可必要 |

## 4. 運用・技術的検証

| 主張 | 判定 | 詳細 |
|------|------|------|
| AnandTech 2024年8月閉鎖 | **正確** | 8/30新規停止、2025年8月アーカイブ削除 |
| CISA RSS 2025年5月12日廃止 | **正確** | GovDeliveryメールに移行 |
| SemiAnalysis $500/yr | **正確** | |
| Anthropic 公式RSSなし | **正確** | コミュニティミラー稼働中 |
| Meta AI 公式RSSなし | **正確** | RSSHub PR #19258 で代替ルート実装済み |
| HuggingFace RSS `<link>`欠落バグ | **正確** | `<guid isPermaLink="true">`のみ |
| OpenAI RSS 非時系列 | **正確** | pubDateソート必要 |
| arXiv URL: `export.arxiv.org` | **不正確** | 正: `rss.arxiv.org/rss/{category}` |
| arXiv 15分ポーリング間隔 | **不正確** | 公式は3秒間隔。日次更新のため実用的には低頻度で可 |
| NIH RSS URL | **不正確** | 正: `nih.gov/news-releases/feed.xml` |
| bioRxiv 30件キャップ | **正確** | |
| bioRxiv TDM API | **正確** | S3バルクアクセス + API提供 |

## 5. プレスワイヤ・政府ラボフィード

| 主張 | 判定 |
|------|------|
| Business Wire フィードオプション | **正確** |
| GlobeNewswire 業界分類 | **正確** |
| PR Newswire カテゴリフィード | **正確** |
| NASA RSS (+ JPL + science.nasa.gov) | **正確** |
| NIH RSS (URLは不正確だがフィード自体は存在) | **部分的** |
| NIST 複数トピックフィード | **正確** |
| MIT News トピック別フィード | **正確** |
| ESA RSS | **正確** |
| ITER RSSなし | **正確** |
| ORNL RSS | **部分的** (Videosフィードは不在) |
| CERN RSS | **部分的** (feedsページは403、実フィードは稼働) |
| 米連邦PD (contractor運営ラボは対象外の可能性) | **部分的** |
