import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TrendsEmptyState } from "./TrendsEmptyState";

describe("TrendsEmptyState", () => {
  it("未生成メッセージを表示する", () => {
    render(<TrendsEmptyState />);
    expect(screen.getByText("該当するワードはありません")).toBeInTheDocument();
  });

  it("次回生成スケジュールを表示する", () => {
    render(<TrendsEmptyState />);
    expect(screen.getByText(/次回の自動生成/)).toBeInTheDocument();
  });
});
