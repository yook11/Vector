// 実行: node --test scripts/test-article-cache.mjs（既存のNext.js依存を使用）。
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { cp, mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
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

test("保存後の通知で、条件別の記事一覧とカテゴリー件数を次回取得から更新する", {
  timeout: 180_000,
}, async (t) => {
  const directory = await mkdtemp(path.join(tmpdir(), "vector-article-cache-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  let revision = 1;
  const reads = new Map();
  const upstream = createServer((req, res) => {
    const url = new URL(req.url, "http://fixture.invalid");
    reads.set(req.url, (reads.get(req.url) ?? 0) + 1);
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/v1/articles") {
      res.end(
        JSON.stringify({
          items: [
            {
              id: revision,
              translatedTitle: `article-${revision}`,
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
          total: revision,
          page: Number(url.searchParams.get("page") ?? 1),
          perPage: 20,
          totalPages: revision,
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
    "src/features/news/api/get-articles.ts",
    "src/features/news/api/get-categories.ts",
    "src/lib/cache/tags.ts",
    "src/lib/env.ts",
    "src/types",
  ];
  for (const name of files) {
    await mkdir(path.dirname(path.join(directory, name)), { recursive: true });
    await cp(path.join(frontend, name), path.join(directory, name), {
      recursive: true,
    });
  }
  await symlink(
    path.join(frontend, "node_modules"),
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
    "export default { cacheComponents: true };\n",
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
    "src/app/snapshot/route.ts",
    `
import { getArticles } from "@/features/news/api/get-articles";
import { getCategories } from "@/features/news/api/get-categories";
export async function GET(request: Request) {
  const params = new URL(request.url).searchParams;
  const [articles, categories] = await Promise.all([
    getArticles({ page: Number(params.get("page") ?? 1), category: params.get("category") ?? "ai" }),
    getCategories(),
  ]);
  return Response.json({ articles, categories });
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

  const reservation = createServer();
  const port = await listen(reservation);
  await new Promise((resolve) => reservation.close(resolve));
  const server = startNext(
    directory,
    ["start", "-H", "127.0.0.1", "-p", String(port)],
    env,
  );
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
    const response = await fetch(`${base}/snapshot?${query}`);
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
  const initialReads = new Map(reads);
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
  for (const query of queries) {
    const fresh = await snapshot(query);
    assert.equal(fresh.articles.items[0].id, 2);
    assert.equal(fresh.categories.items[0].recentCount, 2);
  }
  const refreshedReads = new Map(reads);
  for (const query of queries) await snapshot(query);
  assert.deepEqual(reads, refreshedReads, "再取得した結果もキャッシュされる");
});
