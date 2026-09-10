// ブラウザ検証は RUN_ARTICLE_UPDATE_BROWSER_TEST=1 node --test scripts/test-article-cache.mjs で実行する。
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import {
  cp,
  mkdir,
  mkdtemp,
  realpath,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const frontend = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const nextBin = path.join(frontend, "node_modules/next/dist/bin/next");
const secret = "test-only-article-cache-revalidation-secret";

async function listen(server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return server.address().port;
}

async function writeFixture(directory, name, contents) {
  const destination = path.join(directory, name);
  await mkdir(path.dirname(destination), { recursive: true });
  await writeFile(destination, contents);
}

function startNext(directory, args, env) {
  const child = spawn(process.execPath, [nextBin, ...args], {
    cwd: directory,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (chunk) => {
    output += chunk;
  });
  child.stderr.on("data", (chunk) => {
    output += chunk;
  });
  const closed = once(child, "close");
  return { child, closed, output: () => output };
}

function startStandalone(directory, port, env) {
  const standalone = path.join(
    directory,
    ".next/standalone",
    path.basename(directory),
  );
  const child = spawn(process.execPath, [path.join(standalone, "server.js")], {
    cwd: standalone,
    env: { ...env, HOSTNAME: "127.0.0.1", PORT: String(port) },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (chunk) => {
    output += chunk;
  });
  child.stderr.on("data", (chunk) => {
    output += chunk;
  });
  const closed = once(child, "close");
  return { child, closed, output: () => output };
}

test("保存後の通知で、条件別の記事一覧とカテゴリー件数を次回取得から更新する", {
  timeout: 180_000,
}, async (t) => {
  const dependencyRoot = path.dirname(
    await realpath(path.join(frontend, "node_modules")),
  );
  const directory = await mkdtemp(path.join(dependencyRoot, ".article-cache-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  let revision = 1;
  const reads = new Map();
  let articleGate = null;
  let failArticles = false;
  const upstream = createServer(async (req, res) => {
    const url = new URL(req.url, "http://fixture.invalid");
    reads.set(req.url, (reads.get(req.url) ?? 0) + 1);
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/v1/articles") {
      const articleRevision = revision;
      if (failArticles) {
        res.writeHead(503).end(JSON.stringify({ error: "unavailable" }));
        return;
      }
      if (articleGate) await articleGate.promise;
      res.end(
        JSON.stringify({
          items: [
            {
              id: articleRevision,
              translatedTitle: `article-${articleRevision}`,
              keyPoints: [],
              summaryPreview: null,
              category: {
                slug: url.searchParams.get("category") ?? "ai",
                name: "AI",
              },
              source: { name: "Test source" },
              publishedAt: "2026-09-09T00:00:00Z",
            },
          ],
          total: articleRevision,
          page: Number(url.searchParams.get("page") ?? 1),
          perPage: 20,
          totalPages: articleRevision,
        }),
      );
    } else if (url.pathname === "/api/v1/categories") {
      res.end(
        JSON.stringify({
          items: [{ slug: "ai", name: "AI", recentCount: revision }],
        }),
      );
    } else {
      res.writeHead(404).end();
    }
  });
  const upstreamPort = await listen(upstream);
  t.after(
    () =>
      new Promise((resolve) => {
        upstream.close(resolve);
        upstream.closeAllConnections();
      }),
  );

  // 本番ソースとSDKをコピーし、通信先と認証依存だけをローカルfixtureへ置換する。
  const files = [
    "src/app/api/internal/revalidate/route.ts",
    "src/app/api/news/revision/route.ts",
    "src/features/news/components/ArticleListUpdateNotice.tsx",
    "src/features/news/api/get-articles.ts",
    "src/features/news/api/get-categories.ts",
    "src/lib/cache/article-list-revision.ts",
    "src/lib/cache/tags.ts",
    "src/components/feedback/ErrorMessage.tsx",
    "src/lib/types/error-page.ts",
    "src/lib/env.ts",
    "src/types",
  ];
  for (const name of files) {
    await mkdir(path.dirname(path.join(directory, name)), { recursive: true });
    await cp(path.join(frontend, name), path.join(directory, name), {
      recursive: true,
    });
  }
  await mkdir(path.join(directory, "src/app/snapshot"), { recursive: true });
  await cp(
    path.join(frontend, "src/app/(public)/error.tsx"),
    path.join(directory, "src/app/snapshot/error.tsx"),
  );
  await writeFixture(
    directory,
    "src/components/layout/PageNavigation.tsx",
    "export function PageNavigationReset() { return null; }",
  );
  await writeFixture(
    directory,
    "src/components/ui/button.tsx",
    'import type { ComponentProps } from "react"; export function Button({ variant, size, ...props }: ComponentProps<"button"> & { variant?: string; size?: string }) { return <button {...props} />; }',
  );
  await writeFixture(
    directory,
    "src/app/away/page.tsx",
    'import Link from "next/link"; export default function Away() { return <main><h1>別画面</h1><Link href="/snapshot">戻る</Link></main>; }',
  );
  await symlink(
    path.join(dependencyRoot, "node_modules"),
    path.join(directory, "node_modules"),
    "dir",
  );
  await writeFixture(
    directory,
    "package.json",
    JSON.stringify({ private: true }),
  );
  await writeFixture(
    directory,
    "next.config.mjs",
    `export default { cacheComponents: true, output: "standalone", outputFileTracingRoot: ${JSON.stringify(dependencyRoot)} };\n`,
  );
  await writeFixture(
    directory,
    "tsconfig.json",
    JSON.stringify({
      compilerOptions: {
        target: "ES2022",
        lib: ["dom", "esnext"],
        strict: true,
        noEmit: true,
        module: "esnext",
        moduleResolution: "bundler",
        jsx: "react-jsx",
        esModuleInterop: true,
        resolveJsonModule: true,
        isolatedModules: true,
        skipLibCheck: true,
        paths: { "@/*": ["./src/*"] },
      },
      include: ["**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
      exclude: ["node_modules"],
    }),
  );
  await writeFixture(
    directory,
    "src/lib/api/hey-api.config.ts",
    `
import { requireEnv } from "@/lib/env";
import type { CreateClientConfig } from "@/types/client.gen";
export const createClientConfig: CreateClientConfig = (config) => ({
  ...config, baseUrl: requireEnv("CACHE_TEST_BACKEND_URL"),
});
`,
  );
  await writeFixture(
    directory,
    "src/lib/api/hey-api-interceptors.ts",
    `
export { client as publicClient } from "@/types/client.gen";
`,
  );
  await writeFixture(
    directory,
    "src/app/api/snapshot/route.ts",
    `
import { getArticles } from "@/features/news/api/get-articles";
import { getCategories } from "@/features/news/api/get-categories";
import { getArticleListRevision } from "@/lib/cache/article-list-revision";
export async function GET(request: Request) {
  const params = new URL(request.url).searchParams;
  const revision = getArticleListRevision();
  const [articles, categories] = await Promise.all([
    getArticles({ page: Number(params.get("page") ?? 1), category: params.get("category") ?? "ai" }, revision),
    getCategories(revision),
  ]);
  return Response.json({ revision, articles, categories });
}
`,
  );
  await writeFixture(
    directory,
    "src/app/snapshot/page.tsx",
    `
import { ArticleListUpdateNotice } from "@/features/news/components/ArticleListUpdateNotice";
import { getArticles } from "@/features/news/api/get-articles";
import { getCategories } from "@/features/news/api/get-categories";
import { getArticleListRevision } from "@/lib/cache/article-list-revision";
import { Suspense } from "react";
import Link from "next/link";
export default function SnapshotPage({ searchParams }: { searchParams: Promise<Record<string, string>> }) {
  return <Suspense fallback={<p>loading</p>}><SnapshotContent searchParams={searchParams} /></Suspense>;
}
async function SnapshotContent({ searchParams }: { searchParams: Promise<Record<string, string>> }) {
  const params = await searchParams;
  const revision = getArticleListRevision();
  const articlesPromise = getArticles({ page: Number(params.page ?? 1), category: params.category ?? "ai", sortOrder: params.sortOrder === "asc" ? "asc" : "desc", perPage: Number(params.perPage ?? 20) }, revision);
  const categories = await getCategories(revision);
  return <main>
    <ArticleListUpdateNotice displayedRevision={revision} />
    <p data-testid="revision">{revision}</p>
    <Suspense fallback={<p>記事を更新中…</p>}><Articles articlesPromise={articlesPromise} /></Suspense>
    <Link href="/away">別画面へ</Link>
    <p data-testid="category-count">{categories.items[0]?.recentCount ?? 0}</p>
  </main>;
}
async function Articles({ articlesPromise }: { articlesPromise: ReturnType<typeof getArticles> }) {
 const articles = await articlesPromise;
 return <p data-testid="article">{articles.items[0]?.translatedTitle ?? "empty"}</p>;
}
`,
  );
  await writeFixture(
    directory,
    "src/app/layout.tsx",
    `
export default function Layout({ children }: { children: React.ReactNode }) {
  return <html><body>{children}</body></html>;
}
`,
  );

  // ローカルの秘密情報や設定ファイルを子プロセスに引き継がない。
  const env = {
    PATH: process.env.PATH,
    NODE_ENV: "production",
    NEXT_TELEMETRY_DISABLED: "1",
    REVALIDATE_BEARER_SECRET: secret,
    CACHE_TEST_BACKEND_URL: `http://127.0.0.1:${upstreamPort}`,
  };
  const build = startNext(directory, ["build", "--webpack"], env);
  t.after(() => {
    if (build.child.exitCode === null) build.child.kill("SIGKILL");
  });
  const [buildCode] = await build.closed;
  assert.equal(buildCode, 0, build.output());
  await mkdir(
    path.join(directory, ".next/standalone", path.basename(directory), ".next"),
    {
      recursive: true,
    },
  );
  await cp(
    path.join(directory, ".next/static"),
    path.join(
      directory,
      ".next/standalone",
      path.basename(directory),
      ".next/static",
    ),
    { recursive: true },
  );

  const reservation = createServer();
  const port = await listen(reservation);
  await new Promise((resolve) => reservation.close(resolve));
  let server = startStandalone(directory, port, env);
  t.after(async () => {
    if (server.child.exitCode !== null) return;
    server.child.kill("SIGTERM");
    const timer = setTimeout(() => server.child.kill("SIGKILL"), 5000);
    try {
      await server.closed;
    } finally {
      clearTimeout(timer);
    }
  });
  const base = `http://127.0.0.1:${port}`;
  for (let attempt = 0; ; attempt++) {
    try {
      await fetch(`${base}/not-found`, { signal: AbortSignal.timeout(1000) });
      break;
    } catch {
      assert.ok(
        attempt < 100 && server.child.exitCode === null,
        server.output(),
      );
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }

  async function snapshot(query) {
    const response = await fetch(`${base}/api/snapshot?${query}`);
    assert.equal(response.status, 200, server.output());
    return response.json();
  }
  async function notify(token, tags) {
    return fetch(`${base}/api/internal/revalidate`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ tags }),
    });
  }

  const queries = [
    "page=1&category=ai",
    "page=2&category=ai",
    "page=1&category=computing",
  ];
  for (const query of queries)
    assert.equal((await snapshot(query)).articles.items[0].id, 1);
  const initialRevision = (await snapshot(queries[0])).revision;
  const initialReads = new Map(reads);
  for (let index = 0; index < 10; index++) {
    const response = await fetch(`${base}/api/news/revision`, {
      cache: "no-store",
    });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).revision, initialRevision);
    assert.equal(response.headers.get("cache-control"), "no-store");
  }
  assert.deepEqual(reads, initialReads, "識別子の確認では記事APIを呼ばない");
  revision = 2;
  assert.equal((await notify("wrong-secret", ["articles:list"])).status, 403);
  assert.equal((await notify(secret, [])).status, 400);
  for (const query of queries) {
    const cached = await snapshot(query);
    assert.equal(cached.articles.items[0].id, 1);
    assert.equal(cached.categories.items[0].recentCount, 1);
  }
  assert.deepEqual(reads, initialReads, "通知前は実際にキャッシュが使われる");
  assert.equal((await notify(secret, ["briefing:list", "trends"])).status, 200);
  assert.equal((await snapshot(queries[0])).articles.items[0].id, 1);
  const response = await notify(secret, [
    "articles:list",
    "articles:categories",
  ]);
  assert.equal(response.status, 200, server.output());
  assert.deepEqual(await response.json(), { ok: true, count: 2 });
  assert.deepEqual(reads, initialReads, "通知だけでは取得元にアクセスしない");
  const notifiedRevision = (
    await (
      await fetch(`${base}/api/news/revision`, { cache: "no-store" })
    ).json()
  ).revision;
  assert.notEqual(notifiedRevision, initialRevision);
  for (const query of queries) {
    const fresh = await snapshot(query);
    assert.equal(fresh.revision, notifiedRevision);
    assert.equal(fresh.articles.items[0].id, 2);
    assert.equal(fresh.categories.items[0].recentCount, 2);
  }
  const refreshedReads = new Map(reads);
  for (const query of queries) await snapshot(query);
  assert.deepEqual(reads, refreshedReads, "再取得した結果もキャッシュされる");

  if (process.env.RUN_ARTICLE_UPDATE_BROWSER_TEST === "1") {
    const { chromium, expect } = await import("@playwright/test");
    const browser = await chromium.launch({ headless: true });
    t.after(() => browser.close());
    const page = await browser.newPage();
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.clock.install();
    const initialPoll = page.waitForResponse((response) =>
      response.url().endsWith("/api/news/revision"),
    );
    await page.goto(
      `${base}/snapshot?page=2&category=computing&sortOrder=asc&perPage=10`,
    );
    await expect(page.getByTestId("article")).toHaveText("article-2");
    await initialPoll;
    await expect(page.getByTestId("revision")).toHaveText(notifiedRevision);
    await expect(page.getByText("新しい記事が追加されました")).toHaveCount(0);
    revision = 3;
    await notify(secret, ["articles:list", "articles:categories"]);
    await page.clock.fastForward(60_000);
    await page.getByText("新しい記事が追加されました").waitFor();
    await page.getByRole("button", { name: "一覧を更新" }).click();
    await page.getByText("article-3").waitFor();
    assert.equal(await page.getByTestId("category-count").textContent(), "3");
    assert.match(
      page.url(),
      /page=2&category=computing&sortOrder=asc&perPage=10$/,
    );
    await expect(page.getByRole("button", { name: "更新中…" })).toHaveCount(0);

    // 識別子だけが別プロセスの値を返しても、同じ一覧の更新を完了できる。
    await page.route("**/api/news/revision", (route) =>
      route.fulfill({ json: { revision: "other-process" } }),
    );
    await page.clock.fastForward(60_000);
    await page.getByRole("button", { name: "一覧を更新" }).waitFor();
    await page.unroute("**/api/news/revision");
    await page.getByRole("button", { name: "一覧を更新" }).click();
    await expect(page.getByText("新しい記事が追加されました")).toHaveCount(0);
    await expect(page.getByTestId("article")).toHaveText("article-3");

    // 取得中の追加通知を、表示済みの更新として取り込まない。
    revision = 4;
    await notify(secret, ["articles:list", "articles:categories"]);
    await page.clock.fastForward(60_000);
    articleGate = Promise.withResolvers();
    const readsBeforeRefresh = Array.from(reads)
      .filter(([url]) => url.startsWith("/api/v1/articles"))
      .reduce((sum, [, count]) => sum + count, 0);
    await page.getByRole("button", { name: "一覧を更新" }).click();
    await expect(page.getByRole("button", { name: "更新中…" })).toBeDisabled();
    await expect
      .poll(() =>
        Array.from(reads)
          .filter(([url]) => url.startsWith("/api/v1/articles"))
          .reduce((sum, [, count]) => sum + count, 0),
      )
      .toBeGreaterThan(readsBeforeRefresh);
    await page.getByRole("button", { name: "更新中…" }).dispatchEvent("click");
    revision = 5;
    await notify(secret, ["articles:list", "articles:categories"]);
    articleGate.resolve();
    articleGate = null;
    await expect(page.getByTestId("article")).toHaveText("article-4");
    await page.getByRole("button", { name: "一覧を更新" }).waitFor();
    await page.getByRole("button", { name: "一覧を更新" }).click();
    await expect(page.getByTestId("article")).toHaveText("article-5");

    // 確認失敗は一覧を維持し、次の定期確認で復帰する。
    await page.route("**/api/news/revision", (route) =>
      route.fulfill({ status: 503, body: "unavailable" }),
    );
    await page.clock.fastForward(60_000);
    await expect(page.getByTestId("article")).toHaveText("article-5");
    await expect(page.getByText("新しい記事が追加されました")).toHaveCount(0);
    await page.unroute("**/api/news/revision");
    revision = 6;
    await notify(secret, ["articles:list", "articles:categories"]);
    await page.clock.fastForward(60_000);
    await page.getByRole("button", { name: "一覧を更新" }).waitFor();
    failArticles = true;
    await page.getByRole("button", { name: "一覧を更新" }).click();
    await page
      .getByRole("heading", { name: "ページの読み込みに失敗しました" })
      .waitFor();
    failArticles = false;
    await page.getByRole("button", { name: "再試行", exact: true }).click();
    await expect(page.getByTestId("article")).toHaveText("article-6");

    // 別画面への遷移中は確認を止め、戻ると未表示の新着を検知する。
    let polls = 0;
    page.on("request", (request) => {
      if (request.url().endsWith("/api/news/revision")) polls++;
    });
    await page.getByRole("link", { name: "別画面へ" }).click();
    await page.getByRole("heading", { name: "別画面" }).waitFor();
    const awayPolls = polls;
    await page.clock.fastForward(120_000);
    assert.equal(polls, awayPolls);
    revision = 7;
    await notify(secret, ["articles:list", "articles:categories"]);
    await page.goBack();
    await page.getByRole("button", { name: "一覧を更新" }).waitFor();
    await page.getByRole("button", { name: "一覧を更新" }).click();
    await expect(page.getByTestId("article")).toHaveText("article-7");
    assert.match(
      page.url(),
      /page=2&category=computing&sortOrder=asc&perPage=10$/,
    );
    assert.deepEqual(pageErrors, [], "未処理のブラウザエラーがない");
    await browser.close();
  }

  const revisionBeforeRestart = (
    await (
      await fetch(`${base}/api/news/revision`, { cache: "no-store" })
    ).json()
  ).revision;
  server.child.kill("SIGTERM");
  await server.closed;
  server = startStandalone(directory, port, env);
  for (let attempt = 0; ; attempt++) {
    try {
      const response = await fetch(`${base}/api/news/revision`, {
        cache: "no-store",
        signal: AbortSignal.timeout(1000),
      });
      const restartedRevision = (await response.json()).revision;
      assert.notEqual(restartedRevision, revisionBeforeRestart);
      break;
    } catch {
      assert.ok(
        attempt < 100 && server.child.exitCode === null,
        server.output(),
      );
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
});
