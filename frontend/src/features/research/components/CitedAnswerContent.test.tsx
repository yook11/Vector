import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type {
  ResearchExternalUrlSource,
  ResearchInternalArticleSource,
} from "@/types/types.gen";
import { CitedAnswerContent } from "./CitedAnswerContent";

const externalSource: ResearchExternalUrlSource = {
  kind: "external_url",
  sourceRef: "1",
  url: "https://example.com/report",
  title: "External market report",
  sourceName: "Example Research",
  publishedAt: "2026-07-08T00:00:00Z",
  evidenceClaim: "External evidence claim.",
};

const internalSource: ResearchInternalArticleSource = {
  kind: "internal_article",
  sourceRef: "2",
  articleId: 42,
  title: "Internal analysis article",
  publishedAt: "2026-07-07T00:00:00Z",
};

const deletedInternalSource: ResearchInternalArticleSource = {
  kind: "internal_article",
  sourceRef: "3",
  articleId: null,
  title: "Deleted internal article",
  publishedAt: null,
};

/** 複数要素の中から textContent に部分一致するものを1件探す (見つからなければ即失敗させる)。 */
function findByTextContent(elements: HTMLElement[], text: string): HTMLElement {
  const found = elements.find((element) => element.textContent?.includes(text));
  if (!found) {
    throw new Error(`element containing "${text}" was not found`);
  }
  return found;
}

/** マーカー本文中の badge 一覧を出現順 (DOM 順) の aria-label で取得する。 */
function badgeAriaLabels(): (string | null)[] {
  return screen
    .getAllByRole("button", { name: /^出典 / })
    .map((button) => button.getAttribute("aria-label"));
}

describe("CitedAnswerContent — existing behavior (regression)", () => {
  it("keeps marker order and surrounding text, rendering each as a badge in order", () => {
    const { container } = render(
      <CitedAnswerContent
        content="先頭[[1]][[2]]、末尾にも [[1]]。"
        sources={[externalSource, internalSource]}
      />,
    );

    expect(badgeAriaLabels()).toEqual(["出典 1", "出典 2", "出典 1"]);
    expect(container.textContent).not.toMatch(/\[\[\d+\]\]/);
    expect(container.textContent).toContain("先頭");
    expect(container.textContent).toContain("、末尾にも");
  });

  it("removes unmatched markers while keeping matched ones as badges", () => {
    const { container } = render(
      <CitedAnswerContent
        content="前[[9]]中[[1]]後"
        sources={[externalSource]}
      />,
    );

    expect(container.textContent).not.toContain("[[9]]");
    expect(container.textContent).not.toContain("[[1]]");
    expect(container.textContent).toContain("前");
    expect(container.textContent).toContain("中");
    expect(container.textContent).toContain("後");
    expect(screen.getAllByRole("button", { name: /^出典 / })).toHaveLength(1);
    expect(
      screen.queryByRole("button", { name: "出典 9" }),
    ).not.toBeInTheDocument();
  });

  it("renders multi-digit refs as badges at the start and end of the content", () => {
    const tenSource: ResearchExternalUrlSource = {
      ...externalSource,
      sourceRef: "10",
    };

    const { container } = render(
      <CitedAnswerContent
        content={"[[10]]\n本文末尾[[10]]"}
        sources={[tenSource]}
      />,
    );

    expect(container.textContent).not.toContain("[[10]]");
    expect(container.textContent).toContain("本文末尾");
    expect(screen.getAllByRole("button", { name: "出典 10" })).toHaveLength(2);
  });

  it("passes through content without markers unchanged", () => {
    const { container } = render(
      <CitedAnswerContent
        content="marker のない回答です。"
        sources={[externalSource]}
      />,
    );

    expect(container.textContent).toBe("marker のない回答です。");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("removes all marker-like text when sources are empty", () => {
    const { container } = render(
      <CitedAnswerContent content="direct [[1]] answer" sources={[]} />,
    );

    expect(container.textContent).not.toContain("[[1]]");
    expect(container.textContent).toContain("direct");
    expect(container.textContent).toContain("answer");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders citation badges instead of raw marker text and previews external sources", async () => {
    const user = userEvent.setup();
    render(
      <CitedAnswerContent
        content="市場は拡大しています [[1]]。"
        sources={[externalSource]}
      />,
    );

    expect(screen.queryByText("[[1]]")).not.toBeInTheDocument();
    const badge = screen.getByRole("button", { name: "出典 1" });
    expect(badge).toHaveTextContent("1");

    await user.click(badge);

    expect(screen.getByText("外部")).toBeInTheDocument();
    expect(screen.getByText("External market report")).toBeInTheDocument();
    expect(screen.getByText("Example Research")).toBeInTheDocument();
    expect(screen.getByText("External evidence claim.")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /External market report/ });
    expect(link).toHaveAttribute("href", "https://example.com/report");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("links internal sources to the article page", async () => {
    const user = userEvent.setup();
    render(
      <CitedAnswerContent
        content="社内記事に基づく回答 [[2]]。"
        sources={[internalSource]}
      />,
    );

    await user.click(screen.getByRole("button", { name: "出典 2" }));

    expect(screen.getByText("内部記事")).toBeInTheDocument();
    const link = screen.getByRole("link", {
      name: /Internal analysis article/,
    });
    expect(link).toHaveAttribute("href", "/news/42");
  });

  it("does not render a link for deleted internal articles", async () => {
    const user = userEvent.setup();
    render(
      <CitedAnswerContent
        content="削除済み記事の引用 [[3]]。"
        sources={[deletedInternalSource]}
      />,
    );

    await user.click(screen.getByRole("button", { name: "出典 3" }));

    expect(screen.getByText("Deleted internal article")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Deleted internal article/ }),
    ).not.toBeInTheDocument();
  });

  it("opens previews on hover and closes after leaving the badge", async () => {
    const user = userEvent.setup();
    render(
      <CitedAnswerContent
        content="市場は拡大しています [[1]]。"
        sources={[externalSource]}
      />,
    );

    const badge = screen.getByRole("button", { name: "出典 1" });
    await user.hover(badge);

    expect(
      await screen.findByText("External market report"),
    ).toBeInTheDocument();

    await user.unhover(badge);

    await waitFor(() => {
      expect(
        screen.queryByText("External market report"),
      ).not.toBeInTheDocument();
    });
  });
});

describe("CitedAnswerContent — Markdown rendering contract", () => {
  it("renders GFM heading, list, table, strong, inline code, and code fence as elements", () => {
    const content = [
      "## 見出し",
      "",
      "- 項目1",
      "- 項目2",
      "",
      "| 列A | 列B |",
      "| --- | --- |",
      "| a | b |",
      "",
      "**強調**",
      "",
      "inline は `code` です。",
      "",
      "```",
      "fenced code",
      "```",
    ].join("\n");

    const { container } = render(
      <CitedAnswerContent content={content} sources={[]} />,
    );

    expect(screen.getByRole("heading", { name: "見出し" })).toBeInTheDocument();
    expect(
      screen.getAllByRole("listitem").map((item) => item.textContent),
    ).toEqual(["項目1", "項目2"]);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "列A" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "a" })).toBeInTheDocument();
    expect(container.querySelector("strong")?.textContent).toBe("強調");
    expect(container.querySelector("pre code")?.textContent?.trim()).toBe(
      "fenced code",
    );
    const inlineCode = Array.from(container.querySelectorAll("code")).find(
      (element) => element.parentElement?.tagName !== "PRE",
    );
    expect(inlineCode?.textContent).toBe("code");
  });

  it("renders citation markers inside list items and table cells as badges", () => {
    const content = [
      "- 項目 [[1]]",
      "",
      "| 列 |",
      "| --- |",
      "| セル [[2]] |",
    ].join("\n");

    const { container } = render(
      <CitedAnswerContent
        content={content}
        sources={[externalSource, internalSource]}
      />,
    );

    expect(container.textContent).not.toContain("[[1]]");
    expect(container.textContent).not.toContain("[[2]]");

    const listItem = findByTextContent(screen.getAllByRole("listitem"), "項目");
    expect(
      within(listItem).getByRole("button", { name: "出典 1" }),
    ).toBeInTheDocument();

    const cell = findByTextContent(screen.getAllByRole("cell"), "セル");
    expect(
      within(cell).getByRole("button", { name: "出典 2" }),
    ).toBeInTheDocument();
  });

  it("does not turn a citation marker inside a code fence into a badge", () => {
    const content = ["回答本文 [[1]]", "", "```", "[[1]]", "```"].join("\n");

    const { container } = render(
      <CitedAnswerContent content={content} sources={[externalSource]} />,
    );

    expect(screen.getAllByRole("button", { name: "出典 1" })).toHaveLength(1);
    expect(container.querySelector("pre code")?.textContent?.trim()).toBe(
      "[[1]]",
    );
  });

  it("keeps a single line break as a line break instead of collapsing into one paragraph", () => {
    const { container } = render(
      <CitedAnswerContent content={"行1\n行2"} sources={[]} />,
    );

    expect(container.textContent).toContain("行1");
    expect(container.textContent).toContain("行2");
    expect(container.querySelector("br")).not.toBeNull();
  });

  it("removes a citation marker inside link text without turning it into a badge", () => {
    render(
      <CitedAnswerContent
        content="[詳細 [[1]]](https://example.com/)"
        sources={[externalSource]}
      />,
    );

    const link = screen.getByRole("link", { name: /詳細/ });
    expect(link).toHaveAttribute("href", "https://example.com/");
    expect(link.textContent).not.toContain("[[1]]");
    expect(within(link).queryByRole("button")).not.toBeInTheDocument();
  });

  it("keeps a single-tilde numeric range as literal text instead of strikethrough", () => {
    const { container } = render(
      <CitedAnswerContent
        content="価格は100~200円、容量は50~100GBです。"
        sources={[]}
      />,
    );

    expect(container.querySelector("del")).toBeNull();
    expect(container.textContent).toContain("100~200円");
    expect(container.textContent).toContain("50~100GB");
  });

  it("renders double-tilde text as strikethrough", () => {
    const { container } = render(
      <CitedAnswerContent content="~~取り消し~~" sources={[]} />,
    );

    const strikethrough = container.querySelector("del");
    expect(strikethrough).not.toBeNull();
    expect(strikethrough?.textContent).toBe("取り消し");
  });
});

describe("CitedAnswerContent — security contract", () => {
  it("does not render raw HTML tags as elements and keeps the tag text visible", () => {
    const { container } = render(
      <CitedAnswerContent
        content="<script>alert(1)</script> と <b>太字</b> の混在"
        sources={[]}
      />,
    );

    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
    expect(container.textContent).toContain("<b>太字</b>");
  });

  it("neutralizes a javascript: scheme href instead of leaving it active", () => {
    const { container } = render(
      <CitedAnswerContent
        content="[リンク](javascript:alert(1))"
        sources={[]}
      />,
    );

    // 無効化された href は空文字になり、a 要素は accessible な link role を失うため
    // role query ではなく DOM query で anchor を取得する。
    const link = findByTextContent(
      Array.from(container.querySelectorAll("a")),
      "リンク",
    );
    expect(link.getAttribute("href")).not.toContain("javascript:");
  });

  it("adds target=_blank and rel=noreferrer to a markdown link", () => {
    render(
      <CitedAnswerContent
        content="[外部](https://example.com/x)"
        sources={[]}
      />,
    );

    const link = screen.getByRole("link", { name: "外部" });
    expect(link).toHaveAttribute("href", "https://example.com/x");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("adds target=_blank and rel=noreferrer to a GFM autolink literal", () => {
    render(
      <CitedAnswerContent
        content="参照元は https://example.com/auto を参照。"
        sources={[]}
      />,
    );

    const link = screen.getByRole("link", {
      name: "https://example.com/auto",
    });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("does not render a markdown image, emits no src-bearing element, and keeps alt text visible", () => {
    const { container } = render(
      <CitedAnswerContent
        content="![代替テキスト](https://example.com/pixel.png)"
        sources={[]}
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[src]")).toBeNull();
    expect(container.textContent).toContain("代替テキスト");
  });
});

describe("CitedAnswerContent — heading level and footnote namespace contract", () => {
  it("shifts and clamps heading semantic levels (h1→h3, h2→h4, h3→h5, h4–h6→h6)", () => {
    const content = [
      "# A",
      "",
      "## B",
      "",
      "### C",
      "",
      "#### D",
      "",
      "##### E",
      "",
      "###### F",
    ].join("\n");

    const { container } = render(
      <CitedAnswerContent content={content} sources={[]} />,
    );

    const headingElements = Array.from(
      container.querySelectorAll<HTMLElement>("h1, h2, h3, h4, h5, h6"),
    );
    const tagNameFor = (text: string) =>
      findByTextContent(headingElements, text).tagName;

    expect(tagNameFor("A")).toBe("H3");
    expect(tagNameFor("B")).toBe("H4");
    expect(tagNameFor("C")).toBe("H5");
    expect(tagNameFor("D")).toBe("H6");
    expect(tagNameFor("E")).toBe("H6");
    expect(tagNameFor("F")).toBe("H6");
    expect(container.querySelector("h1")).toBeNull();
    expect(container.querySelector("h2")).toBeNull();
  });

  it("namespaces footnote DOM ids per rendered answer so two answers in the same DOM do not collide", () => {
    const content = "本文[^1]\n\n[^1]: 注記";

    const { container: containerA } = render(
      <CitedAnswerContent content={content} sources={[]} />,
    );
    const { container: containerB } = render(
      <CitedAnswerContent content={content} sources={[]} />,
    );

    const idsOf = (element: HTMLElement) =>
      Array.from(element.querySelectorAll("[id]")).map((node) => node.id);

    const idsA = idsOf(containerA);
    const idsB = idsOf(containerB);

    // footnote 本体・fnref・footnote label 見出しなど、id を持つ要素が実際に生成されている前提を確認する。
    expect(idsA.length).toBeGreaterThan(0);
    expect(idsB.length).toBeGreaterThan(0);
    // Invariant 6: 同一 content から生成した2回答分の id が一つも重複しない。
    expect(idsA.filter((id) => idsB.includes(id))).toEqual([]);

    for (const answerContainer of [containerA, containerB]) {
      const footnoteRefHrefs = Array.from(
        answerContainer.querySelectorAll("a[href^='#']"),
      ).map((anchor) => anchor.getAttribute("href") ?? "");
      expect(footnoteRefHrefs.length).toBeGreaterThan(0);
      for (const href of footnoteRefHrefs) {
        const targetId = href.slice(1);
        // 自分の container 内に解決先 id が存在すること (他方の回答へ誤リンクしない)。
        expect(
          answerContainer.querySelector(`[id="${targetId}"]`),
        ).not.toBeNull();
      }

      const describedByElements = Array.from(
        answerContainer.querySelectorAll("[aria-describedby]"),
      );
      for (const element of describedByElements) {
        const describedBy = element.getAttribute("aria-describedby")?.trim();
        if (!describedBy) {
          continue; // 空・欠落は許容
        }
        for (const describedById of describedBy.split(/\s+/)) {
          // fnref の aria-describedby (footnote label 見出しなど) も自分の container 内で解決すること。
          // label id が回答単位で名前空間化された場合、aria-describedby 未更新のまま宙に浮く regression を防ぐ。
          expect(
            answerContainer.querySelector(`[id="${describedById}"]`),
          ).not.toBeNull();
        }
      }
    }
  });

  it("does not open page-internal footnote fragment links in a new tab", () => {
    const content = "本文[^1]\n\n[^1]: 注記";

    const { container: containerA } = render(
      <CitedAnswerContent content={content} sources={[]} />,
    );
    const { container: containerB } = render(
      <CitedAnswerContent content={content} sources={[]} />,
    );

    for (const answerContainer of [containerA, containerB]) {
      const fragmentLinks = Array.from(
        answerContainer.querySelectorAll("a[href^='#']"),
      );
      // fnref anchor と back-reference anchor が対象。0件は空虚な green になるため fail させる。
      expect(fragmentLinks.length).toBeGreaterThan(0);
      for (const link of fragmentLinks) {
        expect(link).not.toHaveAttribute("target", "_blank");
      }
    }
  });
});
