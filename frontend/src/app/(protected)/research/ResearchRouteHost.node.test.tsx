import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/postcss";
import postcss from "postcss";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
}));

vi.mock("@/features/research/components/ResearchWorkspace", () => ({
  ResearchWorkspace: () => <main>retained workspace</main>,
}));

import {
  ResearchRouteHost,
  ResearchRouteRejectedOutcome,
} from "@/features/research-client";

const GLOBAL_CSS = resolve(process.cwd(), "src/app/globals.css");
const REJECTED_FIRST_PAINT_SELECTOR =
  "[data-research-route-host]:has([data-research-route-rejected]) > :is([data-research-route-initial], [data-research-route-retained])";

describe("ResearchRouteHost rejected server render contract", () => {
  it("SSR markerとroute UIを同じhostに出しcompiled CSSでinitial/retainedをfirst paintから隠す", async () => {
    const markup = renderToStaticMarkup(
      <ResearchRouteHost initialFallback={<p>初期Research skeleton</p>}>
        <section data-testid="rejected-route-ui">
          <ResearchRouteRejectedOutcome />
          Research thread not found.
        </section>
      </ResearchRouteHost>,
    );

    expect(markup).toContain("data-research-route-host");
    expect(markup).toContain("data-research-route-initial");
    expect(markup).toContain("初期Research skeleton");
    expect(markup).toContain("data-research-route-outlet");
    expect(markup).toContain("data-research-route-rejected");
    expect(markup).toContain("hidden");
    expect(markup).toContain("Research thread not found.");

    const source = await readFile(GLOBAL_CSS, "utf8");
    const compiled = await postcss([tailwindcss()]).process(source, {
      from: GLOBAL_CSS,
    });
    const rejectedRule = compiled.root.nodes.find(
      (node) =>
        node.type === "rule" &&
        node.selector.replaceAll(/\s+/g, " ").trim() ===
          REJECTED_FIRST_PAINT_SELECTOR,
    );

    expect(rejectedRule).toBeDefined();
    expect(rejectedRule?.type).toBe("rule");
    if (rejectedRule?.type !== "rule") return;
    expect(
      rejectedRule.nodes.some(
        (node) =>
          node.type === "decl" &&
          node.prop === "display" &&
          node.value === "none",
      ),
    ).toBe(true);
  });
});
