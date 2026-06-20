import { act, fireEvent, render, screen } from "@testing-library/react";
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

  // unstable_retry は内部 transition で refetch するため、押下受理は local の最小可視窓で表す。
  it("押下中は disabled + aria-busy + 「再試行中…」、最小可視時間後に解除される", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.useFakeTimers();
    try {
      render(
        <ErrorMessage
          title="エラー"
          description="説明"
          error={testError}
          unstable_retry={vi.fn()}
        />,
      );

      act(() => {
        fireEvent.click(screen.getByRole("button", { name: "再試行" }));
      });

      const pending = screen.getByRole("button", { name: "再試行中…" });
      expect(pending).toBeDisabled();
      expect(pending).toHaveAttribute("aria-busy", "true");

      act(() => {
        vi.advanceTimersByTime(600);
      });

      const restored = screen.getByRole("button", { name: "再試行" });
      expect(restored).toBeEnabled();
      expect(restored).toHaveAttribute("aria-busy", "false");
    } finally {
      vi.useRealTimers();
    }
  });
});
