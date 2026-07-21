# Development

このドキュメントは、Vector のローカル開発、検証コマンド、CI / security gate、型生成の手順をまとめる。

## Pre-commit

初回 clone 後、commit 前 hook を install する。

```bash
uvx pre-commit install
```

これにより `git commit` 時に gitleaks (secret 検出) / hadolint (Dockerfile lint) / Ruff / Biome が staged diff に対して自動実行される。
CI では Gitleaks が導入 patch と commit message を別々に検査し、Hadolint も再実行するため、`--no-verify` で bypass しても PR で fail する。

## CI security gate

PR / main push では次の blocking gate が自動実行される。詳細設定は各 workflow が正本。

- [`security-pr.yml`](.github/workflows/security-pr.yml) — osv-scanner (lockfile SCA) + npm audit (`--audit-level=high`) + Semgrep CE (`p/owasp-top-ten` + `p/security-audit`)
- [`ci.yml`](.github/workflows/ci.yml) — Ruff / Biome / tsc + unit / integration test + Playwright E2E smoke

公開初期は次の高コストな検査を Actions UI から手動実行する。

- [`security-nightly.yml`](.github/workflows/security-nightly.yml) — Trivy fs / config scan (HIGH+CRITICAL)
- [`schemathesis-nightly.yml`](.github/workflows/schemathesis-nightly.yml) — FastAPI `/openapi.json` と実装の適合性 fuzz (Schemathesis)

自動 gate の新規 finding は PR を block し、手動検査の finding はその run を fail させる。検出結果は Actions Artifacts に退避する。

ローカル再現:

```bash
docker run --rm -v "${PWD}:/src" -w /src ghcr.io/google/osv-scanner:v2 -r ./   # OSV
cd frontend && npm audit --omit=dev --audit-level=high                          # npm audit
pip install semgrep && semgrep --config=p/owasp-top-ten --config=p/security-audit .  # Semgrep
```

## Test / lint

Backend:

```bash
docker compose exec backend ruff check app/
docker compose exec backend ruff format --check app/
docker compose exec backend python -m pytest tests/ -x -q
```

Frontend:

```bash
docker compose exec frontend npx biome check src/
docker compose exec frontend npx tsc --noEmit
docker compose exec frontend npm test
```

## Integration tests

`-m integration` のテストは host から専用 `db-test` (`127.0.0.1` の random port, project 名 `vector-test-<worktree>`) を立てて回す。Makefile が `DATABASE_URL` / `MIGRATION_DATABASE_URL` / role password を OS env で注入するため `.env` は不要。worktree 直下からも `.env` symlink なしで動き、project 名・ポートが worktree ごとに分離されるため複数 worktree で並列実行できる。

```bash
# 全 integration テスト
make test-integration

# 個別ファイル / マーカー絞り込み
make test-integration PYTEST_ARGS='tests/path/to_test.py -q'
make test-integration PYTEST_ARGS='-k "search and quota"'
```

`uv run pytest` を直接叩くと `.env` 不在時に conftest が dummy DB (`unreachable.invalid`) にフォールバックするため、DB 接続が要るテストは必ず `make test-integration` 経由で回す。終了時は `trap` で `down -v --remove-orphans` するため tmpfs ごと毎回 fresh。

## Backend workerのsource反映

`backend/app`のbind mountが保証するのは、container filesystemから最新sourceを読めることだけである。起動済みの常駐Python processが保持するimport cacheは更新されないため、backend app code・設定・ORM modelを変更した後は、次のResearch runを検証する前に標準経路の`make pipeline-restart`でbackend、全worker、schedulerを再生成する。

再生成前に、Research画面で意図したagent runが`queued`または`running`ではないことを確認する。`make pipeline-status`の`agent` queue深度も併せて確認できるが、workerが取得済みのrunはqueue深度に現れないため、queueが空であることだけを停止可否の根拠にしない。active runは終状態まで待ち、workerの強制停止を見かけ上の修復に使わない。

標準の再生成と確認は次の順で行う。

```bash
# 再生成前のcontainer IDと開始時刻を記録
docker inspect --format '{{.Id}} {{.State.StartedAt}}' "$(docker compose ps -q worker-agent)"

make pipeline-restart

# IDまたは開始時刻の更新とrunning状態を確認
docker inspect --format '{{.Id}} {{.State.StartedAt}}' "$(docker compose ps -q worker-agent)"
docker compose ps worker-agent
docker compose logs --since 5m worker-agent
```

再生成後のlogにFATAL、import error、`ThreadMessageSnapshot`または`missing_aspects`の`AttributeError`がないことを確認する。すでに`failed/internal_error`となったrunはDBで修復せず終状態のまま保持し、必要な質問だけをfresh workerへ同じthreadから再送する。

影響範囲が`worker-agent`だけに限定されると確認済みの調査では、active agent runがないことを確認してから次の限定復旧を使える。ただし、通常のsource変更後の検証経路は`make pipeline-restart`とする。

```bash
docker compose up -d --force-recreate worker-agent
docker compose ps worker-agent
docker compose logs --since 5m worker-agent
```

## Type generation

Backend の Pydantic schemas が API contract の正本。変更後は frontend 型を再生成する。

```bash
# Backend 起動中に実行
cd frontend && npm run generate-types
```
