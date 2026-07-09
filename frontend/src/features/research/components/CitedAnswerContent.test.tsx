import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type {
  ResearchExternalUrlSource,
  ResearchInternalArticleSource,
} from "@/types/types.gen";
import {
  CitedAnswerContent,
  parseCitedAnswerContent,
} from "./CitedAnswerContent";

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

function segmentLabels(
  segments: ReturnType<typeof parseCitedAnswerContent>,
): string[] {
  return segments.map((segment) =>
    segment.type === "citation"
      ? `citation:${segment.source.sourceRef}`
      : `text:${segment.text}`,
  );
}

describe("parseCitedAnswerContent", () => {
  it("keeps text order and replaces matching markers with citation segments", () => {
    const segments = parseCitedAnswerContent(
      "先頭[[1]][[2]]、末尾にも [[1]]。",
      [externalSource, internalSource],
    );

    expect(segmentLabels(segments)).toEqual([
      "text:先頭",
      "citation:1",
      "citation:2",
      "text:、末尾にも ",
      "citation:1",
      "text:。",
    ]);
  });

  it("removes unmatched markers without changing surrounding text", () => {
    const segments = parseCitedAnswerContent("前[[9]]中[[1]]後", [
      externalSource,
    ]);

    expect(segmentLabels(segments)).toEqual([
      "text:前",
      "text:中",
      "citation:1",
      "text:後",
    ]);
  });

  it("handles multi-digit markers at line boundaries", () => {
    const tenSource: ResearchExternalUrlSource = {
      ...externalSource,
      sourceRef: "10",
    };

    const segments = parseCitedAnswerContent("[[10]]\n本文末尾[[10]]", [
      tenSource,
    ]);

    expect(segmentLabels(segments)).toEqual([
      "citation:10",
      "text:\n本文末尾",
      "citation:10",
    ]);
  });

  it("passes through content without markers", () => {
    const segments = parseCitedAnswerContent("marker のない回答です。", [
      externalSource,
    ]);

    expect(segmentLabels(segments)).toEqual(["text:marker のない回答です。"]);
  });

  it("removes marker-like text when sources are empty", () => {
    const segments = parseCitedAnswerContent("direct [[1]] answer", []);

    expect(segmentLabels(segments)).toEqual(["text:direct ", "text: answer"]);
  });
});

describe("CitedAnswerContent", () => {
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
