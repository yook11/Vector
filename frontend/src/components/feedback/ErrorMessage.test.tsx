import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ErrorMessage } from "./ErrorMessage";

const testError = new Error("test error");

describe("ErrorMessage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("title・description・再試行ボタンを描画する", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});

    render(
      <ErrorMessage
        title="エラーが発生しました"
        description="しばらくしてから再度お試しください"
        error={testError}
        unstable_retry={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "エラーが発生しました" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("しばらくしてから再度お試しください"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "再試行" })).toBeInTheDocument();
  });

  it("ボタン押下で unstable_retry がちょうど1回・引数なしで呼ばれる", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const retry = vi.fn();
    const user = userEvent.setup();

    render(
      <ErrorMessage
        title="エラー"
        description="説明"
        error={testError}
        unstable_retry={retry}
      />,
    );

    await user.click(screen.getByRole("button", { name: "再試行" }));

    expect(retry).toHaveBeenCalledTimes(1);
    expect(retry).toHaveBeenCalledWith();
  });

  it("初期状態でボタンは enabled で aria-busy が false", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});

    render(
      <ErrorMessage
        title="エラー"
        description="説明"
        error={testError}
        unstable_retry={vi.fn()}
      />,
    );

    const button = screen.getByRole("button", { name: "再試行" });
    expect(button).toBeEnabled();
    expect(button).toHaveAttribute("aria-busy", "false");
  });

  it("retry受付をpendingへ偽装せず単一live statusで通知する", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const retry = vi.fn();
    const user = userEvent.setup();

    render(
      <ErrorMessage
        title="エラー"
        description="説明"
        error={testError}
        unstable_retry={retry}
      />,
    );

    await user.click(screen.getByRole("button", { name: "再試行" }));

    const button = screen.getByRole("button", { name: "再試行" });
    const notices = screen.getAllByRole("status", {
      name: "再試行を開始しました",
    });
    expect(retry).toHaveBeenCalledTimes(1);
    expect(button).toBeEnabled();
    expect(button).not.toHaveAttribute("aria-busy", "true");
    expect(screen.queryByRole("button", { name: "再試行中…" })).toBeNull();
    expect(notices).toHaveLength(1);
    expect(notices[0]).toHaveAttribute("aria-live", "polite");
    expect(notices[0]).toHaveAttribute("aria-atomic", "true");
  });
});
