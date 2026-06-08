import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PaperByline } from "./PaperByline";

describe("PaperByline", () => {
  it("renders the source badge, label, and published date", () => {
    render(
      <PaperByline
        sourceName="Hacker News"
        sourceLabel="Hacker News"
        publishedAt="2026-05-31T02:30:00.000Z"
      />,
    );

    expect(screen.getByText("Y")).toBeInTheDocument();
    expect(screen.getByText("Hacker News")).toBeInTheDocument();
    expect(screen.getByText("2026年5月31日")).toBeInTheDocument();
  });

  it("appends the time only when withTime is set", () => {
    render(
      <PaperByline
        sourceName="Hacker News"
        sourceLabel="Hacker News"
        publishedAt="2026-05-31T02:30:00.000Z"
        withTime
      />,
    );

    // 日付に時刻 (HH:MM) が併記される。tz 依存値は形だけ検証する。
    expect(screen.getByText(/2026年5月31日\s+\d{2}:\d{2}/)).toBeInTheDocument();
  });
});
