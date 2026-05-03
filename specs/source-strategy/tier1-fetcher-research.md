# Tier 1 ソース Fetcher 設計リサーチ (2026-05-03)

Phase 3 で追加予定の Tier 1 全 23 ソースについて、実フィード検証ベースで fetcher 設計に必要な情報を集約した記録。

## 1. 目的

[`production-readiness-2026-05-03.md`](./production-readiness-2026-05-03.md) で確定した Tier 1 GREEN ソース群を Phase 3 で fetcher 化する前に、以下を事前確定する:

- 実 endpoint URL (404 多発)
- Feed format (RSS 2.0 / Atom / RDF / sitemap-only / HTML scrape)
- Pattern R / H 判定 (RSS で full text 取れるか abstract のみか)
- Per-article license / attribution 要件
- Vector 既存 BaseRssFetcher で吸収可能か / 新基底クラスが要るか
- PR 分割の妥当性 (`feedback_phase_pr_split_pattern.md` 適用)

## 2. リサーチ方法

各 PR グループに 1 体ずつ general-purpose subagent を割り当て、`WebFetch` で実フィードと robots.txt を取得して構造を確認。3 ラウンド × 3-4 並列で全 9 グループ調査。

## 3. 横断的発見

### 3.1 PR 数の修正 (元 9 PR → 14 PR)

リサーチ結果、当初の 9 PR グルーピングは複数箇所で構造差分が無視できず分割が必要。

| 当初 PR | 修正後 | 理由 |
|---|---|---|
| 3-c (PLOS+eLife+Frontiers+MDPI) | **3-c-1〜4 の 4 PR** | Atom vs RSS / multi-journal / WAF / entry-level license の差分 |
| 3-d (Cloudflare+DeepMind+Anthropic+OpenAI+Meta+HF) | **3-d-1〜4 の 4 PR** | Anthropic は RSS 不在で sitemap-only / Meta AI は AI tag フィルタ必須 |
| 3-h (文科+経産+総務) | **3-h-1+2 の 2 PR** | 文科/総務は RDF、経産は Atom (feed format 差分) |
| 3-i (ORNL+NASA 補強) | **3-i-1+2 の 2 PR** | ORNL は RSS 全滅で HTML scrape / NASA は本体拡張 |

**合計**: 9 → 14 PR (元 9 ソース → 23 ソース、うち 1 ソース [Anthropic] が sitemap、1 ソース [ORNL] が HTML scrape)

### 3.2 新基底クラスの導入

| 新クラス | 用途 | 初出 PR |
|---|---|---|
| `BaseSitemapFetcher` | sitemap.xml の `<urlset>` から URL 列挙 + lastmod delta fetch | PR 3-d-4 (Anthropic) |
| (検討) `BaseHtmlListingFetcher` | HTML listing → 各 detail を 2 段 fetch | PR 3-i-1 (ORNL) — Hacker News HTML 経路と統合可能性あり |

### 3.3 既存 `rss_parser.py` の改善

総務省 RDF が Shift_JIS 宣言。`feedparser.parse(response.text)` だと httpx の `Content-Type: charset=` 推定に依存し UTF-8 として解釈されて bozo になるリスク。`response.content` (bytes) 経由で feedparser に encoding sniffing させる経路に統一すれば全ソースで安全。

→ **PR 3-h-1 着手時に `tools/rss_parser.py` を bytes 経路に変更** (副作用なし、既存 UTF-8 ソースに影響なし)。

### 3.4 multi-journal/multi-feed 設計の統一原則

「Fetcher = 1 source = 1 endpoint」(`project_ingestion_fetcher_internalized` で確立) を維持。journal/topic 別の複数 feed は以下のいずれか:

- **(A) 別 fetcher class**: Frontiers / MDPI など分類の意味が強いケース。共通ロジックは `_common.py` で composition (継承不可)
- **(C) ClassVar `FEEDS` 巡回**: 同じ source identity 内の単純な複数 endpoint。NASA 補強 / Cornell Chronicle / EUR-Lex がこのパターン

選択基準: NewsSource 行を分けたい (category 紐付け / failure isolation 単位を分けたい) なら (A)、同一 source の internal fan-out なら (C)。

### 3.5 license hardcode 戦略

PLOS / eLife / Frontiers は entry-level `<dc:rights>` を出さない (サイト規約で全記事 CC BY 4.0 統一) → fetcher 側で `FetchedMetadata.license = "CC-BY-4.0"` を hardcode。MDPI のみ entry-level `<dc:rights>` URL を normalize する parser を入れる。arXiv は per-article で license が変わる唯一のケース (Phase 6 で別対応)。

### 3.6 政府標準利用規約 attribution 文言

3 省庁ともサンプル文言で共通フォーマット: `"出典：<省庁名>ホームページ（<トップURL>）"`。Vector は翻訳出力するため改変表示として末尾に `"を翻訳"` を追加するのが法的により安全 (CC BY 4.0 §3(a)(1)(B))。

## 4. PR 別ソース仕様

### PR 3-a: NIST + NSF (米国連邦 PD、構造同型)

| 項目 | NIST | NSF |
|---|---|---|
| ENDPOINT | `https://www.nist.gov/news-events/news/rss.xml` | `https://www.nsf.gov/rss/rss_www_news.xml` |
| Format | RSS 2.0 (UTF-8) | RSS 2.0 (UTF-8) |
| Pattern | H (description 81 chars) | H (description 174 chars + ellipsis) |
| Volume | ~0.2 件/日 | ~0.12 件/日 |
| robots.txt | `*` Allow、Crawl-delay なし | `*` Allow、Crawl-delay なし |
| License | 17 U.S.C. §105 (PD) | 17 U.S.C. §105 (PD) |
| attribution_label | `"NIST"` | `"NSF"` |
| Categories | semi/quantum/ai/security/materials | basic research wide |
| Override | なし | なし |

**統合判定**: BaseRssFetcher 構造同型、`NAME` / `ENDPOINT_URL` ClassVar のみで完結。1 PR。

### PR 3-b: ESA/Hubble + ESA/Webb (Djangoplicity 完全同型)

| 項目 | ESA/Hubble | ESA/Webb |
|---|---|---|
| ENDPOINT | `https://esahubble.org/news/feed/` | `https://esawebb.org/news/feed/` |
| Format | RSS 2.0 (UTF-8) | RSS 2.0 (UTF-8) |
| Pattern | H (description = HTML abstract) | H |
| Volume | ~週 0.3-0.5 件 | ~週 1-2 件 |
| robots.txt | 404 (両 host)、自主規制 12h+ | 404、同 |
| License | CC BY 4.0、active link 必須 | CC BY 4.0、active link 必須 |
| attribution_label | `"ESA/Hubble"` | `"ESA/Webb"` |
| 画像 | `<enclosure>` の `cdn.esahubble.org/.../{ID}.jpg` | `cdn.esawebb.org/.../{ID}.jpg` |

**統合判定**: 完全同型 (Djangoplicity ESA 標準 CMS)。共通基底 `EsaImageReleaseFetcher` を `_common.py` に置き、Hubble/Webb は `NAME` / `ENDPOINT_URL` / `attribution_label` の 3 ClassVar 差し替えのみの薄サブクラス。1 PR。

### PR 3-c: OA 学術 (4 PR 分割)

#### PR 3-c-1: PLOS ONE

- **ENDPOINT**: `https://journals.plos.org/plosone/feed/atom`
- **Format**: **Atom 1.0** (唯一の Atom)、`<content type="html">` に full abstract
- **Pattern**: R (full abstract)
- **Volume**: ~40 entries/day (multi-disciplinary)
- **License**: feed-level のみ (entry-level `<dc:rights>` 不存在) → fetcher で hardcode
- **Author**: 個別 `<author><name>` ブロック多数 (最大 19 名)、CSV 圧縮 helper 必要
- **Crawl-delay**: 30s
- **attribution_label**: `"PLOS ONE · CC BY 4.0"`
- **Categories**: bio (60%) / computing (20%) / その他 (20%)
- **Override**: license 自動 inject、author CSV 圧縮

#### PR 3-c-2: eLife

- **ENDPOINT**: `https://elifesciences.org/rss/recent.xml`
- **Format**: RSS 2.0 + `webfeeds:` namespace
- **Pattern**: R (description = full abstract)
- **Volume**: ~2.2 件/日
- **License**: hardcode CC BY 4.0
- **Author**: `email (Name)` 形式 → Name 抽出 parser 必要
- **Crawl-delay**: 10s
- **attribution_label**: `"eLife · CC BY 4.0"`
- **Categories**: bio 95% (`<category>` で Neuroscience / Cell Biology 等)
- **Override**: license hardcode、`<webfeeds:featuredImage>` (ロゴ固定) は無視、email-prefix author parser

#### PR 3-c-3: Frontiers (multi-journal)

- **ENDPOINT pattern**: `https://www.frontiersin.org/journals/{slug}/rss`
- **対象 slug (初版)**: `artificial-intelligence` / `robotics-and-ai` / `energy-research` / `materials`
- **Format**: RSS 2.0 (UTF-8)
- **Pattern**: R (description = 150-400 語の abstract、Editorial / Correction は空)
- **Volume**: per-journal 0.5-2 件/日
- **License**: hardcode CC BY 4.0
- **Crawl-delay**: 明示なし、defensive 10-15s
- **`<category>`**: 記事種別 (`Original Research` 等) で topic ではない → 無視、journal slug → Vector category を hardcode
- **設計**: journal slug ごとに別 fetcher class (`FrontiersAiFetcher` / `FrontiersRoboticsFetcher` / ...)、`fetchers/frontiers/_common.py` に license/author/category helper 抽出 (composition)
- **attribution_label**: `"Frontiers in {Journal Name} · CC BY 4.0"`

#### PR 3-c-4: MDPI (multi-journal、WAF リスク)

- **ENDPOINT pattern**: `https://www.mdpi.com/{ISSN}/feed`
- **対象 ISSN (初版)**: `1996-1944` Materials / `1996-1073` Energies / `1424-8220` Sensors / `2079-4991` Nanomaterials
- **Format**: RSS 2.0 (公式仕様)、**WebFetch / curl は Cloudflare WAF で 403**
- **Pattern**: R (entry-level `<dc:rights>` で CC BY 4.0 明示、唯一 entry-level license あり)
- **Volume**: journal により 50-100 件/日 (高頻度、cron 30 分以下)
- **PR 着手前 PoC**: backend container 内の `make_safe_async_client` で 1 回 fetch して 200 確認必須。403 継続なら OAI-PMH or Crossref API 経路を検討
- **Override**: UA / Accept ヘッダ強化、`<dc:rights>` URL → enum normalize、`<dc:identifier>` から DOI 抽出
- **設計**: ISSN ごとに別 fetcher class、`fetchers/mdpi/_common.py` に共通 helper
- **attribution_label**: `"MDPI {Journal Name} · CC BY 4.0"`

### PR 3-d: AI 寛容テックメディア / AI 企業公式 (4 PR 分割)

#### PR 3-d-1: Cloudflare Blog + DeepMind

| 項目 | Cloudflare | DeepMind |
|---|---|---|
| ENDPOINT | `https://blog.cloudflare.com/rss/` | `https://deepmind.google/blog/rss.xml` |
| Format | RSS 2.0 + dc/content/atom/media | RSS 2.0 + atom/media |
| Pattern | **R** (content:encoded 4500-8000 chars) | H (description 短) |
| Volume | 7-10 件/週 | 2-3 件/週 |
| robots.txt | `Allow: /` + Content-Signal `ai-train=yes` | `Allow: /` blanket allow |
| Author | `dc:creator` 複数 | なし → 固定 `"Google DeepMind"` |
| Override | なし | link 絶対化 (`urljoin`)、author 固定埋め |
| attribution_label | `"The Cloudflare Blog"` | `"Google DeepMind"` |
| Categories | network/security/ai/dev | ai 単独 |

**統合判定**: 1 PR、Cloudflare zero override + DeepMind 2 行 override。

#### PR 3-d-2: OpenAI + Hugging Face Blog

| 項目 | OpenAI | HF Blog |
|---|---|---|
| ENDPOINT | `https://openai.com/news/rss.xml` | `https://huggingface.co/blog/feed.xml` |
| Format | RSS 2.0 (`/blog/rss.xml` も同内容、canonical は `/news/`) | RSS 2.0 + Atom namespace |
| Pattern | H (description 1-3 文) | H (title/link/pubDate/guid のみ、極端に貧弱) |
| Volume | 8-15 件/週 | 25-35 件/週 |
| robots.txt | `Allow: /` blanket | `Allow: /` blanket |
| Author | なし | URL pattern `/blog/{org}/{slug}` から抽出 |
| Override | category 配列を extras に | URL parser で `attribution_label` を `"Hugging Face Blog ({org})"` 形式に動的化 |
| attribution_label | `"OpenAI News"` | `"Hugging Face Blog"` (default) |
| Categories | ai 100% | ai 100% (org 別 quality 差あり) |

**統合判定**: 1 PR、両方 Pattern H + RSS 2.0 で構造同型。author 抽出ロジックの差異は url-parser に厚いテストで吸収。

#### PR 3-d-3: Meta AI (about.fb.com、AI tag フィルタ必須)

- **ENDPOINT**: `https://about.fb.com/news/feed/` (※ `ai.meta.com/blog` は専用 RSS / sitemap **一切提供なし**)
- **Format**: RSS 2.0 + dc/content/media、WordPress 標準
- **Pattern**: R (`content:encoded` に full body)
- **Volume**: 全社混在 15-25 件/週、AI フィルタ後 5-10 件/週
- **重要**: about.fb.com は AI 以外 (WhatsApp / Sustainability) も流入 → `<category>` 配列に "AI" を含む entry のみ採用するフィルタを `convert_entry` に必須実装
- **robots.txt**: `about.fb.com` は blanket Allow (facebook.com 本体は GPTBot/ClaudeBot Disallow だが本サブドメインは別ポリシー)
- **attribution_label**: `"Meta Newsroom"`
- **Override**: `<category>` AI フィルタ、`dc:creator` ("Facebook" 固定が多い) → author に map
- **既知 limitation**: Llama 等の技術詳細記事は ai.meta.com/blog に出るが Vector では取りこぼす可能性 → spec に明記

**統合判定**: AI フィルタ業務ロジックがクリティカル → 別 PR。

#### PR 3-d-4: Anthropic (sitemap-only、BaseSitemapFetcher 新設)

- **ENDPOINT**: `https://www.anthropic.com/sitemap.xml` (RSS は `/rss.xml` `/feed` `/news/rss.xml` 全て 404)
- **Format**: sitemap.xml (urlset, lastmod 付与)
- **Volume**: ~3-5 件/週 (`/news/` ~380 entries)
- **robots.txt**: `Allow: /` blanket + sitemap 明示
- **設計**: `BaseRssFetcher` 不適合 → 新基底 `BaseSitemapFetcher` を導入
  - `fetch()`: sitemap.xml パース → `<loc>` が `/news/` で始まる URL を `<lastmod>` 降順抽出
  - 各 URL を `extract_html_body` で本文取得
  - 状態管理: `lastmod` ベースで delta fetch
- **attribution_label**: `"Anthropic"`
- **将来再利用**: HF Meta AI で sitemap 経路が必要になったときに再利用可能

**統合判定**: 独自基盤、固有テスト厚め。別 PR。

### PR 3-e: Cornell Chronicle (multi-topic feed)

- **Top-level ENDPOINT 不在**: `/rss.xml` `/all/rss.xml` `/feed` 全て 404。Drupal taxonomy 経由のみ
- **採用 endpoints (taxonomy term ID)**:
  - `/taxonomy/term/24043/feed` (AI)
  - `/taxonomy/term/14256/feed` (Computing & Information Sciences)
  - `/taxonomy/term/15056/feed` (Life Sciences & Veterinary Medicine)
  - `/taxonomy/term/15621/feed` (Energy, Environment & Sustainability)
  - `/taxonomy/term/14252/feed` (Physical Sciences & Engineering)
  - `/taxonomy/term/14248/feed` (Health, Nutrition & Medicine)
- **Format**: RSS 2.0 (UTF-8)、pubDate RFC822 (`EDT/EST` 含む)
- **Pattern**: H (description 158-346 chars summary、`content:encoded` 全 feed で欠落)
- **Volume**: 1.3 件/日 (top-level 推定)
- **robots.txt**: feed パス Allow、GPTBot Crawl-delay 3s、ClaudeBot 等は無記載 (default Allow)
- **`dc:creator`**: 内部 ID (`kah53` 等) 解決不能 → author=None に落とす
- **attribution_label**: `"Cornell Chronicle"` (policy が文言指定なし)
- **設計**: 選択肢 (C) `FEEDS: ClassVar[tuple[(term_id, category_hint), ...]]` 巡回。重複は `articles.url` UNIQUE + `on_conflict_do_nothing` で吸収

**統合判定**: 1 fetcher class で 6 feed 巡回。

### PR 3-f: EU JRC + EMA + IAEA (Drupal 系、Pattern H 統一)

| 項目 | JRC | EMA | IAEA |
|---|---|---|---|
| ENDPOINT | `https://joint-research-centre.ec.europa.eu/node/2/rss_en` | `https://www.ema.europa.eu/en/news.xml` | `https://www.iaea.org/feeds/topnews` (※ `/feeds/news` は 402 で不安定) |
| Format | RSS 2.0 / Drupal | RSS 2.0 / Drupal | RSS 2.0 / Drupal |
| Pattern | H (desc 100-160 chars) | H (desc 180-2800 chars) | **H (desc 完全に空)** |
| pubDate | RFC822 標準 | RFC822 標準 | **独自 `yy-mm-dd HH:MM`** + `+0200` (CET) 付与 |
| Volume | 10-15 件/週 | 2-5 件/週 | 5-10 件/週 |
| License | CC BY 4.0 (Decision 2011/833/EU) | EMA 利用条件 (商用 OK) | "freely available" + 商用 OK |
| attribution_label | `"European Commission · JRC (CC BY 4.0)"` | `"European Medicines Agency"` | `"International Atomic Energy Agency"` |
| Categories | 多領域 (ai/energy/bio/materials/mobility/security/space) | bio 100% | energy 70% / security 25% / bio 5% |
| Override 固有 | 多 category 写像辞書 | 固定 bio | pubDate parse + URL `http→https` + URL/title category 推定 |

**統合判定**: 1 PR、3 ソース共通 RSS 2.0 + Pattern H + Drupal。差分は `convert_entry` 内に閉じる。テストは IAEA pubDate parser と JRC category mapping に厚く。

### PR 3-g: EUR-Lex (CELEX prefix pre-filter)

- **ENDPOINT**: `https://eur-lex.europa.eu/EN/display-feed.rss?rssId={N}` (predefined 9 本のみ public)
- **Custom RSS は不可**: My EUR-Lex 登録ユーザの個人 token 紐付け、public な CELEX 単位 RSS は提供されない
- **採用 feed**:
  - rssId=162 (Parliament/Council legislation, 主)
  - rssId=165 (OJ L 個別 acts, implementing acts カバー)
  - rssId=161 (Commission proposals, 補助)
- **Format**: RSS 2.0 + dc namespace
- **Pattern**: H (description 空、本文は CELEX URL から HTML 抽出)
- **Volume**: rssId=162 は月数十件、rssId=165 は週数十-数百件
- **License**: CC BY 4.0 (編集 + summaries + consolidated texts)、metadata は CC0
- **attribution_label**: `"EUR-Lex · © European Union (CC BY 4.0)"`
- **多言語**: URL の `/EN/` 固定で英語 feed
- **重要設計**: `TARGET_CELEX_PREFIXES: ClassVar` に AI Act / NIS2 / CRA / DSA / DORA / EHDS / MDR / CBAM の prefix を持たせ、`<title>` 先頭の `CELEX:xxxxx:` を抽出して prefix match → 該当のみ Stage A 投入。LLM 分類負荷を 99% 削減
- **CELEX prefix 例** (実装時 ClassVar):
  - `32024R1689` AI Act
  - `32022R2554` DORA
  - `32022R2065` DSA
  - `32022L2555` NIS2
  - `32024R2847` CRA
  - `32023R0988` CBAM
  - `32025R0327` EHDS
  - `32017R0745` MDR
- **Override**: `<title>` から CELEX 分離して `external_id` 化、`<link>` の `/./` 正規化、CELEX prefix → vector_category dict 内蔵

**統合判定**: 1 fetcher で複数 feed 巡回 + CELEX prefix filter。

### PR 3-h: 日本省庁 (2 PR 分割: format 差異)

#### PR 3-h-1: 文部科学省 + 総務省 (RDF)

| 項目 | MEXT | MIC |
|---|---|---|
| ENDPOINT | `https://www.mext.go.jp/b_menu/news/index.rdf` | `https://www.soumu.go.jp/news.rdf` |
| Format | RSS 1.0 (RDF) | RSS 1.0 (RDF) |
| Encoding | UTF-8 | **Shift_JIS** ← 唯一の非 UTF-8 |
| Pattern | H (description 空) | H (description = title、本文ゼロ) |
| Volume | ~30-50 件/週 | ~30-50 件/週 |
| robots.txt | 404 / 60s 間隔安全 | `ia_archiver` のみ Disallow |
| License | 政府標準利用規約 2.0 + CC BY 4.0 互換 | 政府標準利用規約 + ODC-By v1.0 互換 |
| attribution_label | `"出典：文部科学省ホームページ（https://www.mext.go.jp/）を翻訳"` | `"出典：総務省ホームページ（https://www.soumu.go.jp/）を翻訳"` |
| Categories | bio/ai/space/policy | security/ai/policy |
| Override | なし (feedparser 標準対応) | summary == title なら summary を捨てる |

**前提改善**: PR 3-h-1 着手時に `tools/rss_parser.py` の `feedparser.parse(response.text)` を `response.content` 経由に変更 (Shift_JIS 対応 + 全 UTF-8 ソースに副作用なし)。

**統合判定**: 1 PR、RDF 形式が同一。MIC の Shift_JIS は rss_parser.py 改善で吸収。

#### PR 3-h-2: 経済産業省 (Atom)

- **ENDPOINT**: `https://www.meti.go.jp/ml_index_release_atom.xml`
- **Format**: **Atom 1.0** (3 省庁中唯一)
- **Encoding**: UTF-8 推定
- **Pattern**: H 想定 (実装時 PoC で確認)
- **Volume**: 20-40 件 (新着情報)
- **License**: 政府標準利用規約 2.0 + PDL 1.0
- **attribution_label**: `"出典：経済産業省ホームページ（https://www.meti.go.jp/）を翻訳"`
- **Categories**: energy/ai (DX)/mobility/policy
- **接続性リスク**: WebFetch / curl で接続不能、本番 worker から `User-Agent: VectorBot/1.0` 明示で再検証必須
- **Override**: Atom 専用 fallback (feedparser は同一 API、`entry.published_parsed` / `entry.updated_parsed` 両対応)

**統合判定**: Atom + 接続性リスクで別 PR。固有 PoC をまず実施。

### PR 3-i: ORNL + NASA 補強 (2 PR 分割: 設計が真逆)

#### PR 3-i-1: ORNL (HTML scrape 専用)

- **RSS 全滅**: `/rss/news.xml` `/news/feed` `/feed` 全て 404、`/rss.xml` は news ではなく landing page hybrid
- **ENDPOINT**: `https://www.ornl.gov/news` (HTML listing)
- **設計**: `BaseHtmlListingFetcher` 経路 (Hacker News HTML 経路と統合可能性あり)
  - listing から `a[href^="/news/"]` で URL 列挙
  - 各 detail page を 2 段 fetch (`requests` + `selectolax` or 既存 `extract_html_body`)
- **Volume**: 推定 2-5 件/週
- **robots.txt**: `Crawl-delay: 10`、`/news/` Allow
- **License**: "we will not assert its rights against you"、credit line "Courtesy of Oak Ridge National Laboratory, U.S. Department of Energy" 必須
- **attribution_label**: `"ORNL · DOE"` (短縮、UI で full credit を template 展開)
- **Categories**: materials / computing (HPC) / energy
- **Override**: HTML scrape 全部、Drupal site 構造前提

**統合判定**: BaseRssFetcher 不適合の唯一の Tier 1 ソース、別 PR、固有テスト厚め。

#### PR 3-i-2: NASA 補強 feed 群 (本体 fetcher 拡張)

5 補強 feed:
- `https://www.nasa.gov/news-release/feed/` (~30 件/月)
- `https://www.nasa.gov/technology/feed/` (~10-15 件/月、ai/computing/materials/space-tech)
- `https://www.nasa.gov/aeronautics/feed/` (~8-12 件/月、mobility)
- `https://www.nasa.gov/missions/station/feed/` (~25-30 件/月、ISS)
- `https://www.nasa.gov/missions/artemis/feed/` (~15-20 件/月、Moon)

**全 5 feed が本体 `/feed/` と完全同型** (RSS 2.0 + content:encoded + dc:creator + media:content + category、WordPress 6.9.4)、pubDate RFC822。

**重複の実測**: 本体 feed と news-release/artemis/station の各 feed で URL 重複が確実に発生。

**設計**: 既存 `NasaFetcher` を拡張、`FEEDS: ClassVar[tuple[str, ...]]` で 6 URL (本体 + 補強 5) を保持
- `fetch_entries()` を override: 6 feed を順次 `feedparser.parse` → `chain.from_iterable` で flat → `seen_urls: set[str]` で in-memory dedup → 既存 `convert_entry` に流す
- `convert_entry` は本体と完全同型のため override 不要
- `attribution_label`: 既存 NASA のままで良い (feed 別に変えない)
- ENDPOINT_URL: 代表として `/feed/` を残し、`FEEDS` ClassVar に 6 URL 列挙

**統合判定**: 既存 fetcher の拡張 1 ファイルで完結、別 PR (本体 fetcher の挙動変更を独立に PR レビュー)。

## 5. Phase 3 修正後 PR ロードマップ

| PR | 内容 | fetcher 数 | 工数感 | 並列性 |
|---|---|---|---|---|
| 3-a | NIST + NSF | 2 | 小 | 独立 |
| 3-b | ESA Hubble + Webb (`_common.py` 共通基底) | 2 | 小 | 独立 |
| 3-c-1 | PLOS ONE | 1 | 小 | 独立 |
| 3-c-2 | eLife | 1 | 小 | 独立 |
| 3-c-3 | Frontiers (4 journal class + `_common.py`) | 4 | 中 | 独立 |
| 3-c-4 | MDPI (4 ISSN class + `_common.py` + WAF PoC) | 4 | 中-大 | PoC 後着手 |
| 3-d-1 | Cloudflare + DeepMind | 2 | 小 | 独立 |
| 3-d-2 | OpenAI + HF Blog | 2 | 小 | 独立 |
| 3-d-3 | Meta AI (AI フィルタ) | 1 | 中 | 独立 |
| 3-d-4 | Anthropic (`BaseSitemapFetcher` 新設) | 1 | 中-大 | 基底新設で先行推奨 |
| 3-e | Cornell Chronicle (FEEDS 巡回) | 1 | 中 | 独立 |
| 3-f | JRC + EMA + IAEA | 3 | 中 | 独立 |
| 3-g | EUR-Lex (CELEX prefix filter) | 1 | 中 | 独立 |
| 3-h-1 | MEXT + MIC (RDF) + `rss_parser.py` 改善 | 2 | 中 | 先行推奨 (rss_parser 改善は他に影響なし) |
| 3-h-2 | METI (Atom + 接続性 PoC) | 1 | 中 | PoC 後着手 |
| 3-i-1 | ORNL (HTML scrape) | 1 | 中-大 | 独立 |
| 3-i-2 | NASA 補強 (`FEEDS` 巡回拡張) | 0 (既存拡張) | 小 | 独立 |

**計**: 14 PR、新規 fetcher 27 (Frontiers/MDPI が multi-class)、既存拡張 1 (NASA)

**推奨着手順**:
1. **PR 3-h-1** (rss_parser.py 改善が他 PR の前提になり得る)
2. **PR 3-d-4** (BaseSitemapFetcher 新設、将来再利用基盤)
3. 残りは並列着手可

## 6. Open Questions

1. **MDPI WAF (PR 3-c-4)**: backend container の `make_safe_async_client` で 200 取れるか PR 着手前 PoC 必須。403 継続なら OAI-PMH or Crossref API 経路に切替
2. **METI 接続性 (PR 3-h-2)**: 同上、本番 worker から `User-Agent` 明示で疎通確認
3. **`BaseHtmlListingFetcher` の統合 (PR 3-i-1)**: 既存 `HackerNewsFetcher` の HTML 経路と共通基底化するか、別実装にするか
4. **EUR-Lex CELEX prefix list の更新運用 (PR 3-g)**: 新法令採択時に prefix を追加する手順を spec or memory に記録
5. **Frontiers / MDPI の追加 journal**: 初版 4 journal だが、後で追加する場合の手順 (alembic migration + `JOURNAL_*` 定数追加 + composition root 1 行)

## 7. 関連

- [production-readiness-2026-05-03.md](./production-readiness-2026-05-03.md) — Tier 1 GREEN 全体カバレッジマトリクス + Phase 計画 (本書の元)
- `feedback_phase_pr_split_pattern.md` — 構造同型は 1 PR、固有挙動は別 PR の判定基準
- `project_ingestion_fetcher_internalized.md` — Fetcher 内在化 NAME/ENDPOINT_URL ClassVar 設計
- `project_collection_redesign_phase1_research.md` — Phase 1 既存 19 ソース実測 (Pattern R/H 分類起点)
- `project_collection_redesign_data_inventory.md` — FetchedArticle Tier 1/2/3 フィールド分類
- `feedback_on_conflict_no_target.md` — 重複 URL の race recovery pattern
- `feedback_source_specific_config_in_module.md` — ソース固有定数は fetcher モジュールで完結
- `feedback_aggregate_over_individual_vo.md` — 不変条件はアグリゲート単位で保証
- `feedback_briefing_design_lessons.md` — テーブル設計は Pydantic schema 写しでなくドメイン単位
