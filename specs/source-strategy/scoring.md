# スコアリングフレームワークとマスターリスト

## スコアリング5軸 (100点満点)

| 軸 | 配点 | 説明 |
|----|------|------|
| 編集品質・信頼性 | 30点 | 編集体制、業界での権威、ジャーナリスティック・インテグリティ |
| 法的安全性 | 25点 | ToSの明確性、商用利用可否、再配信許諾の強さ |
| コンテンツ配信 | 20点 | フルテキストRSS > スニペット > HTMLスクレイピング必要 |
| 分野カバレッジ | 15点 | Vectorの10分野への貢献度、横断性 |
| 更新頻度 | 10点 | 毎日更新 > 週数件 > 月数件 |

### 法的安全性の配点ルール

Vectorの運用形態 (内部解析 + 引用範囲表示) を前提とした配点:

| 点数 | 条件 | 例 |
|------|------|-----|
| 25点 | 公的パブリックドメイン | NASA, NIH, NIST |
| 22点 | 明示オープンライセンス | arXiv, bioRxiv, ESA (CC-BY) |
| 20点 | 標準著作権 + 引用範囲で問題なし | VentureBeat, Krebs 等 |
| 15点 | ToSにグレー/paywall/anti-scrape条項あるが引用範囲なら許容 | TechCrunch, Engadget |
| 10点 | 改変・翻案禁止を明記 (要約生成にリスク残存) | ITmedia系, Impress系 |
| 5点 | 全体がnon-commercial明記 (引用でもリスク高) | NVIDIA Developer Blog |

### 配点に関する設計判断

- **コンテンツ配信 (20点) は過度にペナライズしない**: Vector には Trafilatura パイプラインが稼働済みのため、スニペットRSSからフル本文取得の限界コストはほぼゼロ
- **法的安全性はソース品質としては重要だが、ユーザー価値とは別軸**: NIH (82点) が TechCrunch (73点) より高スコアになる現象は、このフレームワークの限界。Phase選定ではドメインバランスとユーザー価値を別途考慮する

## マスターリスト

### S-Tier (85-100点)

| # | ソース | 点数 | 言語 | 主要分野 | フィードURL | コンテンツ |
|---|--------|------|------|---------|-----------|----------|
| 1 | IEEE Spectrum | 91 | EN | 全10分野 | `spectrum.ieee.org/feeds/feed.rss` | Full |
| 1 | NASA | 91 | EN | Space/Material | `nasa.gov/news-release/feed/` | Full (PD) |
| 3 | Microsoft Research | 90 | EN | AI/Computing | `microsoft.com/en-us/research/feed/` | Full |
| 4 | bioRxiv | 88 | EN | Biotech | `connect.biorxiv.org/biorxiv_xml.php?subject={cat}` | Abstract+link |

### A-Tier (75-84点)

| # | ソース | 点数 | 言語 | 主要分野 | フィードURL | コンテンツ |
|---|--------|------|------|---------|-----------|----------|
| 5 | Krebs on Security | 84 | EN | Security | `krebsonsecurity.com/feed/` | Full |
| 6 | Schneier on Security | 83 | EN | Security/Crypto | `schneier.com/feed/atom/` | Full (Atom) |
| 7 | VentureBeat | 82 | EN | AI | `venturebeat.com/feed/` | Full |
| 7 | NIH News | 82 | EN | Biotech/Medical | `nih.gov/news-releases/feed.xml` | Full (PD) |
| 9 | ESA | 81 | EN | Space | `esa.int/rssfeed/Our_Activities` | Full (CC-BY) |
| 9 | Spaceflight Now | 81 | EN | Space | `spaceflightnow.com/feed/` | Full |
| 11 | arXiv | 80 | EN | 全研究分野 | `rss.arxiv.org/rss/{category}` | Abstract |
| 12 | JPCERT/CC | 79 | JA | Security | `jpcert.or.jp/rss/jpcert.rdf` | Summary |
| 13 | JAXA press | 78 | JA | Space | `jaxa.jp/rss/press_j.rdf` | Headline+link |
| 13 | NIST | 78 | EN | Security/Materials | `nist.gov/news-events/news/rss.xml` | Summary (PD) |

### B-Tier (65-74点)

| # | ソース | 点数 | 言語 | 主要分野 | フィードURL | 法的注記 |
|---|--------|------|------|---------|-----------|---------|
| 15 | ITmedia NEWS | 74 | JA | AI/Semi/Security | `rss.itmedia.co.jp/rss/2.0/news_bursts.xml` | 改変禁止条項 |
| 16 | ITmedia AI+ | 74 | JA | AI | `rss.itmedia.co.jp/rss/2.0/aiplus.xml` | 同上 |
| 17 | TechCrunch | 73 | EN | AI/Tech全般 | `techcrunch.com/feed/` | Snippet、anti-scrape ToS |
| 18 | MONOist | 72 | JA | Robotics/Manufacturing | `rss.itmedia.co.jp/rss/2.0/monoist.xml` | 改変禁止条項 |
| 18 | The Register | 72 | EN | Semi/Security | `theregister.com/headlines.atom` | Snippet |
| 20 | EE Times Japan | 71 | JA | Semi | `rss.itmedia.co.jp/rss/2.0/eetimes.xml` | 改変禁止条項 |
| 21 | SpaceNews | 70 | EN | Space | `spacenews.com/feed/` | Snippet |
| 21 | Hacker News API | 70 | EN | 全分野 (シグナル) | `hacker-news.firebaseio.com/v0/topstories.json` | JSON |
| 23 | スマートジャパン | 69 | JA | Energy | `rss.itmedia.co.jp/rss/2.0/smartjapan.xml` | 改変禁止条項 |
| 24 | Engadget | 68 | EN | Mobility/Gadget | `engadget.com/rss.xml` | Full、Yahoo ToS |
| 25 | The Record | 68 | EN | Security (国家主体) | `therecord.media/feed` | Snippet |
| 25 | The Quantum Insider | 68 | EN | Next-gen Computing | `thequantuminsider.com/feed/` | Snippet |
| 27 | Semiconductor Eng. | 67 | EN | Semi | `semiengineering.com/feed/` | Cloudflare要確認 |
| 28 | PC Watch | 66 | JA | Semi/Computing | `pc.watch.impress.co.jp/data/rss/1.0/pcw/feed.rdf` | Impress著作権 |
| 29 | Payload Space | 66 | EN | Space | `payloadspace.com/feed/` | Snippet |
| 29 | CleanTechnica | 66 | EN | Energy | `cleantechnica.com/feed/` | Snippet |
| 31 | INTERNET Watch | 65 | JA | Networks/Security | `internet.watch.impress.co.jp/data/rss/1.0/iw/feed.rdf` | Impress著作権 |
| 31 | Electrek | 65 | EN | EV/Energy | `electrek.co/feed/` | Snippet |
| 31 | Ars Technica | 65 | EN | Science/Space/AI | `feeds.arstechnica.com/arstechnica/index` | 2段落paywall |
| 31 | World Nuclear News | 65 | EN | Nuclear | `world-nuclear-news.org/rss` | Snippet |

### C-Tier (55-64点)

| # | ソース | 点数 | 言語 | 主要分野 | 備考 |
|---|--------|------|------|---------|------|
| 35 | Nikkei xTECH | 62 | JA | 全分野 | mostly paywall |
| 36 | Google Project Zero | 62 | EN | Security 0-day | 頻度低、質最高峰 |
| 37 | CNET Japan | 61 | JA | Tech全般 | |
| 37 | Google DeepMind | 61 | EN | AI | 研究所公式、頻度低 |
| 39 | OpenAI | 60 | EN | AI | 非時系列問題 |
| 39 | Hugging Face Blog | 60 | EN | AI | `<link>`欠落バグ |
| 41 | GIGAZINE | 59 | JA | Tech全般 | 比較的長い抜粋あり |
| 41 | Import AI | 59 | EN | AI | Substack週刊 |
| 43 | HPCwire | 58 | EN | HPC | 業界紙 |
| 44 | Fierce Network | 57 | EN | Networks | Networks貴重 |
| 45 | ASCII.jp | 57 | JA | Tech全般 | |
| 46 | GEN | 56 | EN | Biotech | 二次媒体 |

### D-Tier (50-54点)

| # | ソース | 点数 | 言語 | 状態 |
|---|--------|------|------|------|
| 47 | Quanta Magazine | 56 | EN | CC誤認、通常著作権 |
| 48 | PR TIMES | 54 (→75) | JA | メディア登録審査次第 |
| 49 | Chips and Cheese | 55 | EN | Substack週刊 |
| 50 | Phoronix | 55 | EN | Linux/HW |
| 51 | SemiAnalysis | 54 | EN | $500/yr paid |
| 52 | Endpoints News | 53 | EN | Partial paywall |
| 53 | STAT News | 52 | EN | STAT+ paywall |
| 54 | RIKEN | 52 | JA | 転載に正式許可必要 |
