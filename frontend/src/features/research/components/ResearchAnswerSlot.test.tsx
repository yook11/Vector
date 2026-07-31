import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ResearchAssistantMessage } from "@/types/types.gen";
import { ResearchAnswerSlot } from "./ResearchAnswerSlot";

afterEach(() => {
  cleanup();
});

function finalAnswer(
  overrides: Partial<
    Pick<ResearchAssistantMessage, "content" | "sources" | "missingAspects">
  > = {},
): ResearchAssistantMessage {
  return {
    role: "assistant",
    seq: 2,
    content: "確定した回答",
    createdAt: "2026-07-13T00:01:00Z",
    sources: [],
    missingAspects: [],
    ...overrides,
  };
}

function slot(): HTMLElement {
  return screen.getByTestId("research-answer-slot");
}

describe("ResearchAnswerSlot", () => {
  it("does not render a missing aspects notice even when missingAspects is non-empty", () => {
    render(
      <ResearchAnswerSlot
        finalAnswer={finalAnswer({
          missingAspects: ["企業の一次情報", "地域別の内訳"],
        })}
      />,
    );

    expect(
      within(slot()).queryByText("確認できなかった点"),
    ).not.toBeInTheDocument();
    expect(within(slot()).queryByRole("list")).not.toBeInTheDocument();
    expect(
      within(slot()).queryByText("企業の一次情報"),
    ).not.toBeInTheDocument();
    expect(within(slot()).queryByText("地域別の内訳")).not.toBeInTheDocument();
  });

  it("renders identical slot markup whether or not missingAspects is populated", () => {
    render(
      <ResearchAnswerSlot finalAnswer={finalAnswer({ missingAspects: [] })} />,
    );
    const withoutMissingHtml = slot().innerHTML;
    cleanup();

    render(
      <ResearchAnswerSlot
        finalAnswer={finalAnswer({
          missingAspects: ["企業の一次情報", "地域別の内訳"],
        })}
      />,
    );

    expect(slot().innerHTML).toBe(withoutMissingHtml);
  });

  it("still renders the answer body and a citation badge when missingAspects is non-empty", () => {
    render(
      <ResearchAnswerSlot
        finalAnswer={finalAnswer({
          content: "確定した回答 [[1]]。",
          sources: [
            {
              kind: "external_url",
              sourceRef: "1",
              url: "https://example.com/final-source",
              title: "確定ソース",
              sourceName: "Example",
              publishedAt: "2026-07-13T00:00:00Z",
              evidenceClaim: "確定回答を裏付ける情報",
            },
          ],
          missingAspects: ["企業の一次情報"],
        })}
      />,
    );

    expect(slot().textContent).toContain("確定した回答");
    expect(screen.queryByText("[[1]]")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "出典 1" })).toBeInTheDocument();
  });

  it("renders live draft children and marks the slot busy while finalAnswer is null", () => {
    render(
      <ResearchAnswerSlot finalAnswer={null}>
        <p>一時下書き</p>
      </ResearchAnswerSlot>,
    );

    expect(screen.getByText("一時下書き")).toBeInTheDocument();
    expect(slot()).toHaveAttribute("aria-busy", "true");
  });

  it("does not mark the slot busy once the final answer has arrived", () => {
    render(<ResearchAnswerSlot finalAnswer={finalAnswer()} />);

    expect(slot()).not.toHaveAttribute("aria-busy");
  });
});
