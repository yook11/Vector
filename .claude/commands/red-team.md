---
description: ホワイトハッカー人格の主エージェントを起動して本アプリを攻撃させる。デプロイ前 / リリース前の **repo 全体網羅レビュー** で使う (PR 範囲に scope を絞らない、cold zones も同等に攻める)。
---

<!--
.claude/agents/ は Claude Code セッション起動時に 1 回スキャンされる。
エージェント定義を新規追加した直後は再起動しないと subagent_type が認識されない。
-->

## 設計思想

- 主エージェント (`red-team-attacker`) は **仮説駆動ループ** で動く。記憶は Notebook に persist、context が満ちたら世代交代
- サブエージェント: `red-team-probe` (コード読解検証) / `red-team-research` (外部 CVE / 既知パターン)
- **既知パターンの事前注入はしない** (バイアス回避)。stack info は「地図」のみ、playbook 注入禁止
- **Phase 2 は state を変えない**。destructive PoC は report に残し user が手動実行

## Phase 0: Recon (~30秒)

1. セッションディレクトリ作成: `mkdir -p .red-team/$(date -u +%Y%m%dT%H%M%SZ)` (以後 `<session>`)
2. `cp .claude/red-team/notebook_template.md <session>/notebook.md` & `cp .claude/red-team/handoff_brief_template.md <session>/handoff_brief.md`
3. **Test fixture 確認**: `.claude/red-team/test_accounts.local.json` の `user.password` がプレースホルダなら案内して abort:
   ```
   1. dev compose を起動 (docker compose up -d)
   2. /api/auth/sign-up/email で 1 アカウント作成 (redteam-user@local)
   3. .claude/red-team/test_accounts.local.json にメールとパスワードを記入
   4. /red-team を再実行
   ```
   admin fixture は持たない (admin 経路は構造的事実で完結。実観察が必要なら撤退して report に PoC で残す)
4. **Stack & deploy_state を notebook に書く** — 以下を読んで `attack_knowledge.stack` / `attack_knowledge.deploy_state` 節へ:
   - root + frontend + backend + backend/tests の `CLAUDE.md`
   - `frontend/package.json` / `backend/pyproject.toml` (framework + version)
   - `docker-compose.yml` (service / network / port topology)
   - auto memory `MEMORY.md` の Deployment 系エントリ (deploy_state 用)
5. **hot_zones を notebook に書く** — `git log --oneline -20` + `git diff main...HEAD --stat` から直近変更領域を抽出
6. **scanner 並列起動** — Agent ツールで以下を 1 メッセージ複数 tool call で並列起動。**起動 prompt で「scope: repo 全体」を指定**:
   - `injection-scanner` / `auth-and-access-scanner` / `idor-scanner` / `secrets-scanner` / `supply-deploy-scanner` / `exfil-scanner`
7. **Scanner-summary 圧縮** — orchestrator (= /red-team を実行する Claude 自身) が scanner output を要約して `map.scanner_summary` 節へ書く。findings 同士の関係性 (連鎖の種) を積極的に書き、evidence pointer (file:line) は残す

## Phase 1: Attack Loop (max 3 世代)

各世代:
1. `red-team-attacker` を Agent ツールで起動。プロンプトに渡す:
   - `<session>/notebook.md` の絶対パス
   - `<session>/handoff_brief.md` の絶対パス
   - 現在の `generation` 番号 (1〜3)
   - probe + research 合計呼出上限 `probe_budget=25`
2. attacker が内部ループを回し handoff_brief を更新して退場
3. 続行判定: `generation < 3` かつ open_leads が残る → 次世代を起動。それ以外 → Phase 2 へ

## Phase 2: Verify (read-only / 縮小スケール)

- **禁止**: state 変更系 (DB UPDATE/DELETE/INSERT, Redis FLUSHDB, container restart, 並列 flood, 大規模配列)
- **許容**: TTL で自然消滅する観察 / 構造的事実の Read 再確認 / fixture login で GET/OPTIONS 観察 / 縮小スケール (例: `[1]*10` で queue 投入観察)
- **範囲外の chain**: report に PoC として残し user 判断

手順:
1. ローカル docker compose が起動中か確認 (`docker compose ps`)。停止中ならユーザに確認してから起動
2. `attack_tree` の再現性 high chain を選び、handoff_brief の `Verification approach` 節に従って確証を取る
3. `.claude/settings.local.json` allowlist 内の Bash のみ使用。allowlist 外が必要になった chain は report に「未検証 PoC」として残して撤退
4. 結果を `<session>/verified_chains.md` に保存

## Phase 3: Report

`<session>/report.md` に書く:
- 実行サマリ (世代数 / probe 呼出数 / 検出 chain 数 / 所要時間)
- 各 verified chain: 攻撃ゴール / ペルソナ / 前提 (confirmed_facts への参照) / 観察結果 / severity / 修正方向
- **未検証だが懸念高 chain (構築済 PoC)**: 事前準備 / コマンド本体 / 期待観察ポイント / ロールバック
- Phase 0 scanner findings の要約

最後に `<session>/report.md` の絶対パスをユーザに伝える。

## 制約

- 本番 DB / 本番 API への攻撃禁止。Phase 2 はローカル docker compose のみ
- 認証情報・本物の API キーをペイロードに使わない (test fixture のみ)
- destructive PoC は構築のみ、自動実行はしない (report に残す)
