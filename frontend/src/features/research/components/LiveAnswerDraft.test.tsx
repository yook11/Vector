import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LiveAnswerDraft } from "./LiveAnswerDraft";

describe("LiveAnswerDraft", () => {
  it.each([
    "empty",
    "suppressed",
  ] as const)("renders no assistant draft for active %s mode", (draftMode) => {
    const { container } = render(
      <LiveAnswerDraft
        status="running"
        draftMode={draftMode}
        draftText=""
        errorCode={null}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("shows a readable assistant draft only after visible text arrives", () => {
    render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText="調査結果の下書きです。"
        errorCode={null}
      />,
    );

    const label = screen.getByText("回答を生成中…");
    const text = screen.getByText("調査結果の下書きです。");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(label.closest("[aria-live]")).toBeNull();
    expect(label).not.toContainElement(text);
    expect(text.closest("[aria-live]")).toBeNull();
    expect(label.closest('[aria-busy="true"]')).not.toBeNull();
  });

  it("marks the generating spinner decorative and reduced-motion safe", () => {
    render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText="本文"
        errorCode={null}
      />,
    );

    const spinner = document.querySelector('[aria-hidden="true"].animate-spin');
    expect(spinner).not.toBeNull();
    expect(spinner).toHaveClass("animate-spin");
    expect(spinner).toHaveClass("motion-reduce:animate-none");
  });

  it("keeps a visible draft while the completed result is finalizing", () => {
    render(
      <LiveAnswerDraft
        status="completed"
        draftMode="visible"
        draftText="確定待ちの下書き"
        errorCode={null}
      />,
    );

    expect(screen.getByText("回答を確定しています…")).toBeInTheDocument();
    expect(screen.getByText("確定待ちの下書き")).toBeInTheDocument();
  });

  it("shows only the finalizing label when a completed draft was suppressed", () => {
    render(
      <LiveAnswerDraft
        status="completed"
        draftMode="suppressed"
        draftText="復活させない本文"
        errorCode={null}
      />,
    );

    expect(screen.getByText("回答を確定しています…")).toBeInTheDocument();
    expect(screen.queryByText("復活させない本文")).not.toBeInTheDocument();
  });

  it.each([
    ["cancelled", "キャンセルしました"],
    ["enqueue_failed", "実行キューに投入できませんでした"],
    ["stale", "時間切れになりました"],
    ["generation_unavailable", "回答を生成できませんでした"],
    ["internal_error", "回答を生成できませんでした"],
    [null, "回答を生成できませんでした"],
  ] as const)("hides failed draft and projects %s to a safe fixed message", (errorCode, message) => {
    render(
      <LiveAnswerDraft
        status="failed"
        draftMode="visible"
        draftText="残してはいけない下書き"
        errorCode={errorCode}
      />,
    );

    expect(
      screen.queryByText("残してはいけない下書き"),
    ).not.toBeInTheDocument();
    expect(screen.getByText(message)).toBeInTheDocument();
  });

  it("renders received text as escaped React text without filtering citations", () => {
    const text = '<img src="x" onerror="alert(1)"> 本文 [[1]]';
    const { container } = render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText={text}
        errorCode={null}
      />,
    );

    expect(screen.getByText(text)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(text)).toHaveTextContent("本文 [[1]]");
  });

  it("does not move focus when draft text or finalizing state changes", () => {
    const focusTarget = document.createElement("button");
    focusTarget.textContent = "focus target";
    document.body.append(focusTarget);
    focusTarget.focus();

    const { rerender } = render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText="最初"
        errorCode={null}
      />,
    );
    rerender(
      <LiveAnswerDraft
        status="completed"
        draftMode="visible"
        draftText="最初と続き"
        errorCode={null}
      />,
    );

    expect(document.activeElement).toBe(focusTarget);
    focusTarget.remove();
  });

  it("draftTextだけを追記しても既存Markdown paragraphのDOM nodeを維持する", () => {
    const initialText = ["既存の先頭段落", "", "続く段落"].join("\n");
    const { rerender } = render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText={initialText}
        errorCode={null}
      />,
    );
    const existingParagraph = screen.getByText("既存の先頭段落");

    rerender(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText={`${initialText}\n\n追記された段落`}
        errorCode={null}
      />,
    );

    expect(screen.getByText("既存の先頭段落")).toBe(existingParagraph);
    expect(screen.getByText("追記された段落")).toBeInTheDocument();
  });
});

describe("LiveAnswerDraft — Markdown rendering contract (draft)", () => {
  it("renders a shifted heading, a list item, and a code fence as elements", () => {
    const content = [
      "## 見出し",
      "",
      "- 項目",
      "",
      "```",
      "fenced text",
      "```",
    ].join("\n");

    const { container } = render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText={content}
        errorCode={null}
      />,
    );

    const heading = screen.getByRole("heading", { name: "見出し" });
    expect(heading.tagName).toBe("H4");
    expect(screen.getByRole("listitem")).toHaveTextContent("項目");
    expect(container.querySelector("pre code")?.textContent?.trim()).toBe(
      "fenced text",
    );
  });

  it("closes an unterminated emphasis marker via remend and drops the ** from view", () => {
    const content = "これは**重要";

    const { container } = render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText={content}
        errorCode={null}
      />,
    );

    const strong = container.querySelector("strong");
    expect(strong).not.toBeNull();
    expect(strong?.textContent).toBe("重要");
    expect(container.textContent).not.toContain("**");
  });

  it("renders trailing text after an unclosed code fence as a code block", () => {
    const content = ["本文の続き", "", "```", "fenced content"].join("\n");

    const { container } = render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText={content}
        errorCode={null}
      />,
    );

    const codeBlock = container.querySelector("pre code");
    expect(codeBlock).not.toBeNull();
    expect(codeBlock?.textContent).toContain("fenced content");
  });

  it("keeps a complete citation marker and an incomplete fragment as literal text without a badge", () => {
    const content = "引用[[1]]と断片[[1";

    const { container } = render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText={content}
        errorCode={null}
      />,
    );

    expect(container.textContent).toContain(content);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("LiveAnswerDraft — security contract (draft)", () => {
  it("keeps raw HTML tags escaped as visible text", () => {
    const content = "<script>alert(1)</script>";

    const { container } = render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText={content}
        errorCode={null}
      />,
    );

    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText(content)).toBeInTheDocument();
  });

  it("does not render a markdown image element and shows only the alt text", () => {
    const content = "![代替テキスト](https://example.com/x.png)";

    const { container } = render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText={content}
        errorCode={null}
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("代替テキスト")).toBeInTheDocument();
  });

  it("adds target=_blank and rel=noreferrer to a markdown link", () => {
    const content = "[外部](https://example.com/x)";

    render(
      <LiveAnswerDraft
        status="running"
        draftMode="visible"
        draftText={content}
        errorCode={null}
      />,
    );

    const link = screen.getByRole("link", { name: "外部" });
    expect(link).toHaveAttribute("href", "https://example.com/x");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });
});
