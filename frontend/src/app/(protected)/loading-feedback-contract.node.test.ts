import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

function routeSource(relativePath: string): string {
  return readFileSync(
    fileURLToPath(new URL(relativePath, import.meta.url)),
    "utf8",
  );
}

function routeExists(relativePath: string): boolean {
  return existsSync(fileURLToPath(new URL(relativePath, import.meta.url)));
}

function expectLiveVisibleFallback(source: string, label: string): void {
  expect(source).toContain('role="status"');
  expect(source).toContain('aria-live="polite"');
  expect(source).toContain('aria-atomic="true"');
  expect(source).toContain(label);
  expect(source).toContain('aria-hidden="true"');
  expect(source).toContain("motion-reduce:animate-none");
}

describe("application loading feedback contract", () => {
  it("does not use protected or admin route-level loading boundaries", () => {
    expect(routeExists("./loading.tsx")).toBe(false);
    expect(routeExists("./(shell)/(admin)/settings/loading.tsx")).toBe(false);
    expect(
      routeExists("./(shell)/(admin)/admin/pipeline-status/loading.tsx"),
    ).toBe(false);
    expect(
      routeExists("./(shell)/(admin)/admin/source-health/loading.tsx"),
    ).toBe(false);
  });

  it("accepts only the E2E-owned distDir input for fresh feature-data probes", () => {
    const nextConfig = readFileSync(
      resolve(process.cwd(), "next.config.js"),
      "utf8",
    );

    expect(nextConfig).toContain("process.env.E2E_NEXT_DIST_DIR");
    expect(nextConfig).toContain("distDir");
    expect(nextConfig).toContain(".e2e-next");
  });

  it("keeps the font mock outside the Next distDir and cleans both artifacts", () => {
    const runner = readFileSync(
      resolve(process.cwd(), "e2e/fixtures/feature-data-runner.ts"),
      "utf8",
    );

    expect(runner).toContain(".next-font-google-responses.cjs`;");
    expect(runner).toContain(
      "Feature-data font responses must be a sibling of the dist directory",
    );
    expect(runner.match(/await cleanupFeatureDataArtifacts\(/g)).toHaveLength(
      2,
    );
    expect(runner).toContain("rm(directory, { force: true, recursive: true })");
    expect(runner).toContain("rm(fontResponsesPath, { force: true })");
  });

  it("keeps dashboard and watchlist controls inside a visible fallback", () => {
    const dashboard = routeSource("./page.tsx");
    const watchlist = routeSource("./(shell)/watchlist/page.tsx");

    expect(dashboard).not.toContain("fallback={null}");
    expect(dashboard).not.toContain('<span className="h-5" />');
    expect(watchlist).not.toContain("fallback={null}");
    expect(dashboard).toContain("記事を更新中…");
    expect(watchlist).toContain("ウォッチリストを読み込み中…");
  });

  it("renders a primary news-detail fallback before article data and keeps related articles secondary", () => {
    const source = routeSource("./news/[id]/page.tsx");

    expect(source).toContain("NewsDetailSkeleton");
    expect(source).toContain("<Suspense fallback={<NewsDetailSkeleton />}");
    expect(source.indexOf("NewsDetailSkeleton")).toBeLessThan(
      source.indexOf("RelatedArticlesSkeleton"),
    );
    expectLiveVisibleFallback(source, "記事を読み込み中…");
  });

  it("uses feature-local visible, reduced-motion fallbacks for the remaining first viewports", () => {
    expectLiveVisibleFallback(
      routeSource("./(shell)/(main)/briefing/page.tsx"),
      "Briefingを読み込み中…",
    );
    expectLiveVisibleFallback(
      routeSource("./briefing/[category]/page.tsx"),
      "Briefingを読み込み中…",
    );
    expectLiveVisibleFallback(
      routeSource("./(shell)/(main)/trends/page.tsx"),
      "トレンドを読み込み中…",
    );
    expectLiveVisibleFallback(
      routeSource("./(shell)/(admin)/settings/page.tsx"),
      "設定を読み込み中…",
    );
    expectLiveVisibleFallback(
      routeSource("./(shell)/(admin)/admin/pipeline-status/page.tsx"),
      "パイプライン状況を読み込み中…",
    );
    expectLiveVisibleFallback(
      routeSource("./(shell)/(admin)/admin/source-health/page.tsx"),
      "ソース健全性を読み込み中…",
    );
  });

  it("owns the research shell and private-data-free initial fallback in research/layout", () => {
    expect(routeExists("./research/layout.tsx")).toBe(true);

    const layout = routeSource("./research/layout.tsx");
    const entryPage = routeSource("./research/page.tsx");
    const threadPage = routeSource("./research/[threadId]/page.tsx");

    expect(layout).toContain("PaperSurface");
    expect(layout).toContain("ShellMasthead");
    expect(layout).toContain("ResearchWorkspaceSkeleton");
    expectLiveVisibleFallback(layout, "Researchを読み込み中…");
    expect(entryPage).not.toContain("PaperSurface");
    expect(entryPage).not.toContain("ShellMasthead");
    expect(threadPage).not.toContain("PaperSurface");
    expect(threadPage).not.toContain("ShellMasthead");
  });
});
