---
description: ホワイトハッカー人格の主エージェントを起動して本アプリを攻撃させる。デプロイ前 / リリース前の **repo 全体網羅レビュー** で使う (PR 範囲に scope を絞らない、cold zones も同等に攻める)。
---

<!--
Subagent registry の注意:
.claude/agents/ は Claude Code セッション起動時に 1 回スキャンされる。
直前にエージェント定義を新規追加した場合は、再起動しないと subagent_type='red-team-attacker' 等が認識されない。
その場合は再起動してから本コマンドを再実行すること。
-->

## 設計思想

- 主エージェント (`red-team-attacker`) は **仮説駆動ループ** で動く: 仮説を立て、サブエージェントで検証し、事実を Notebook に蓄え、繋がりを探して攻撃 chain を構築する
- 主エージェントの記憶はすべて **Notebook ファイル** (世代を跨いで保持)。context が満ちたら世代交代する
- サブエージェント (`red-team-probe` = コード読解、`red-team-research` = 外部知識) は構造化済の事実を返す。形は問わない (JSON 厳格指定はしない)
- **既知パターンの事前注入はしない**: 攻撃者にバイアスをかけないため、playbook 等のテンプレ攻撃カタログは渡さない。スタック情報のみ与えて、攻撃面の想像と仮説生成は主エージェントの自発に任せる
- **数値閾値での自動制御は最小限**: 終了条件の `probe_budget=25` 以外の数値ルールは持たない。仮説の取捨・委任判断は主エージェント自身が文脈から決める
- **Phase 2 は destructive 操作を行わない**: skill から書込み系 PoC を排除し、構造的事実 + 縮小スケール観察 + read-only 経路で chain を検証する。書込み系は report に PoC として残し、user が手動で実行するかを判断する

## Phase 0: Recon (~30秒)

1. セッションディレクトリ作成: `mkdir -p .red-team/$(date -u +%Y%m%dT%H%M%SZ)`
   - 以後このパスを `<session>` と呼ぶ
2. `cp .claude/red-team/notebook_template.md <session>/notebook.md`
3. `<session>/handoff_brief.md` を空ファイルとして touch
4. **Test fixture の存在確認**: `.claude/red-team/test_accounts.local.json` の `user.password` がプレースホルダ (`<set this manually>`) のままなら、以下を案内して **Phase 1 に進まず abort**:
   ```
   初回セットアップが必要です:
   1. dev compose を起動 (docker compose up -d)
   2. /api/auth/sign-up/email で 1 アカウント作成 (redteam-user@local)
   3. .claude/red-team/test_accounts.local.json にメールとパスワードを記入
   4. /red-team を再実行
   ```
   admin 経路の chain は構造的事実 (コード読解) で完結させる方針なので、admin fixture は持たない。
   admin endpoint の入力を実観察したくなった場合は撤退して report に PoC として残す。
5. **Stack 把握** — 以下を **すべて** 読み出して Notebook の `attack_knowledge.stack` 節へ
   subsection 分割で整形して書き込む。attacker は probe で `node_modules/` や venv の
   ライブラリ source を直読できる前提なので、stack info は **「どこを攻めるべきかの地図」**
   に徹する。CVE 列挙 / 既知 pattern 名 / 攻撃ヒントは書かない (バイアス回避):

   読込対象:
   - `CLAUDE.md` (root) — プロジェクト憲法 / 開発ルール / SSoT
   - `frontend/CLAUDE.md` — frontend 固有の慣用 (proxy.ts / Server Actions / cache 戦略 等)
   - `backend/CLAUDE.md` — backend 固有の慣用 (Depends / SQLModel / taskiq 等)
   - `backend/tests/CLAUDE.md` — テスト文化 (どこに穴が出やすいか副次情報)
   - `frontend/package.json` — Next.js / React / Better Auth / 周辺 lib の正確な version
   - `backend/pyproject.toml` — FastAPI / SQLModel / Pydantic v2 / 周辺 lib の version
   - `docker-compose.yml` — service / networks / ports の topology (どこが host expose / internal-only か)

   書込み subsection (推奨):
   - `frontend` — framework + version + 慣用
   - `backend` — framework + version + 慣用
   - `auth / session` — Better Auth + 内部 JWT の境界
   - `data / DB` — DB user / schema split / migration 経路
   - `infra / runtime` — docker compose の network 境界 + host expose 一覧

   外部 CVE / advisory の事前注入は **行わない**。必要なら attacker が loop 中に
   `red-team-research` を能動呼出する (attacker.md 判断軸どおり)。
6. **Hot zones** — `git log --oneline -20` と `git diff main...HEAD --stat` から直近変更領域を抽出し、
   Notebook の `hot_zones` 節へ書き込む。直近変更は **検査価値の高いシグナルの一つ** として記録するが、
   攻撃面の優先順位は hot_zones に閉じない。**cold zones (直近触っていない領域 = 過去の設計欠陥が
   そのまま残っている可能性) を見落とさない** ことを attacker に明示する (notebook の hot_zones 節
   コメントに「これは優先順位ではなくシグナル」と添える)。
7. **scanner 並列起動** — Agent ツールで以下を **並列 (1 メッセージで複数 tool call)** 起動。
   各 scanner には起動 prompt で **「/red-team Phase 0 モード: repo 全体を網羅レビュー、`git diff`
   範囲に絞らない」** を明示する (scanner 標準動作は PR diff scope なので、この上書きが必須):
   - `injection-scanner` — Injection / Prompt Injection
   - `auth-and-access-scanner` — AuthN / Session / CSRF / CORS / SSRF
   - `idor-scanner` — Authz / IDOR / 所有権 / 横移動
   - `secrets-scanner` — 秘密情報 hardcode / log・bundle 漏出
   - `supply-deploy-scanner` — 依存 / docker-compose / Dockerfile / CI / migration
   - `exfil-scanner` — Response over-fetch / error leak / log leak / debug 露出
8. **Scanner-summary 圧縮** — orchestrator (= /red-team を実行する Claude 自身) が scanner の生 output を要約して Notebook の `map.scanner_summary` 節へ書込む。形は問わない (sidecar は作らない)。次の hacker (= attacker) に有益な形にすることだけを意識:
   - findings 同士の関係性 (連鎖の種) があれば積極的に書く
   - 冗長な rationale は捨てる、evidence pointer (file:line) は必ず残す
   - 生 output のコピペは避ける

## Phase 1: Attack Loop (max 3 世代)

`generation` を 1 から開始。各世代:

1. `red-team-attacker` を Agent ツールで起動。プロンプトに以下を渡す:
   - `<session>/notebook.md` の絶対パス
   - `<session>/handoff_brief.md` の絶対パス (世代 2 以降は前世代執筆済み)
   - 現在の `generation` 番号
   - probe + research 合計呼出上限 (`probe_budget=25`)
2. attacker が内部ループを回し、終了時に `<session>/handoff_brief.md` を更新して退場
3. Notebook を確認:
   - `attack_tree` に再現性 high の chain が出ているか
   - `open_leads` が空か (枯渇)
4. 続行判定:
   - `generation < 3` かつ leads が残る → 次世代を起動
   - それ以外 → Phase 2 へ

## Phase 2: Verify (read-only / 縮小スケール)

destructive 操作 (DB UPDATE/DELETE/INSERT, Redis FLUSHDB, container restart, 並列 flood, 大規模配列) は本フェーズでは **行わない**。代わりに:

1. Notebook の `attack_tree` から再現性 high の chain を選ぶ
2. ローカル docker compose 環境が起動中か確認 (`docker compose ps`)。停止中ならユーザに確認してから起動
3. 各 chain について以下のいずれかで検証:
   - **構造的事実の再確認**: 該当コードを Read で再読、設定ファイルの該当行を grep。「fact レベルの確証」だけで足りる chain はここで終わる
   - **fixture login + read-only 観察**: `.claude/red-team/test_accounts.local.json` を読んで `redteam-user` で login (`/api/auth/sign-in/email`)、cookie を取って GET / OPTIONS で観察。書込みは行わない
   - **縮小スケール観察**: 例えば `source_ids=[1]*10000` ではなく `[1]*10` で endpoint を叩き「Pydantic validate を通過し worker queue に乗る」事実だけを観察。本当に巨大スケールが必要な検証は report に PoC として残す
4. allowlist (`.claude/settings.local.json`) に登録済の Bash パターンのみ使用する。allowlist 外のコマンドが必要になった時点で、その chain は report に「未検証だが懸念高 chain (構築済 PoC)」として記録して撤退
5. 検証結果を `<session>/verified_chains.md` に保存

## Phase 3: Report

`<session>/report.md` に以下を書く:
- 実行サマリ (世代数 / probe 呼出数 / 検出 chain 数 / 所要時間)
- 各 verified chain: 攻撃ゴール / ペルソナ / 前提 (confirmed_facts への参照) / 観察結果 / severity / 修正方向
- **未検証だが懸念高 chain (構築済 PoC)**: Phase 2 で実行を見送った destructive PoC を載せる。ユーザが手動実行するかを判断できる粒度で:
  - 必要な事前準備
  - PoC コマンド本体
  - 期待される観察ポイント
  - ロールバック手順
- Phase 0 scanner findings の要約

最後に `<session>/report.md` の絶対パスをユーザに伝える。

## 制約

- **本番 DB / 本番 API への攻撃禁止**。Phase 2 の検証はローカル docker compose 環境のみ
- 認証情報・本物の API キーをペイロードに使わない (test fixture のみ使用)
- Phase 2 で `rm` / DB 全消去 / 外部送信を伴う PoC は実行禁止 (構築のみで採用、report に書く)
- destructive な検証が必要になったら撤退して report に PoC として残す。skill から自動実行はしない
