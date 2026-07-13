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
});
