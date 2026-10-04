import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  WatchlistButton: vi.fn(() => null),
}));

vi.mock("@/features/watchlist", () => ({
  WatchlistButton: mocks.WatchlistButton,
}));

import { ArticleList } from "./ArticleList";

describe("ArticleList", () => {
  it("renders the paper empty state inside the list area", () => {
    render(<ArticleList items={[]} watchedIds={new Set()} />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("記事がありません");
    expect(status).toHaveTextContent(
      "カテゴリや並び順を変えて、もう一度確認してください。",
    );
    expect(status.parentElement).toHaveClass("border-b");
  });
});
