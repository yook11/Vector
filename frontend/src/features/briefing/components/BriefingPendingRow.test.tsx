import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { PendingCategory } from "../page-models/briefing-list";
import { BriefingPendingRow } from "./BriefingPendingRow";

describe("BriefingPendingRow — 空リスト", () => {
  it("pending が空のとき何も描画しない (「準備中」見出しなし)", () => {
    const { container } = render(<BriefingPendingRow pending={[]} />);
    expect(screen.queryByText("準備中")).not.toBeInTheDocument();
    expect(container.firstChild).toBeNull();
  });
});

describe("BriefingPendingRow — チップ表示", () => {
  it("pending 1 件のとき name と「近日」ラベルが表示される", () => {
    const pending: PendingCategory[] = [{ id: 1, name: "スペース" }];
    render(<BriefingPendingRow pending={pending} />);
    expect(screen.getByText("スペース")).toBeInTheDocument();
    expect(screen.getByText("近日")).toBeInTheDocument();
    expect(screen.getByText("準備中")).toBeInTheDocument();
  });

  it("pending N 件のときチップが N 個表示される", () => {
    const pending: PendingCategory[] = [
      { id: 1, name: "スペース" },
      { id: 2, name: "バイオ" },
      { id: 3, name: "エネルギー" },
    ];
    render(<BriefingPendingRow pending={pending} />);

    // 各カテゴリ名が存在する
    expect(screen.getByText("スペース")).toBeInTheDocument();
    expect(screen.getByText("バイオ")).toBeInTheDocument();
    expect(screen.getByText("エネルギー")).toBeInTheDocument();

    // 「近日」ラベルはチップ数と同じだけ出る
    const nearbyLabels = screen.getAllByText("近日");
    expect(nearbyLabels).toHaveLength(pending.length);
  });

  it("pending 2 件のとき「準備中」セクション見出しが 1 つ出る", () => {
    const pending: PendingCategory[] = [
      { id: 1, name: "スペース" },
      { id: 2, name: "バイオ" },
    ];
    render(<BriefingPendingRow pending={pending} />);
    // 見出しは 1 箇所のみ
    expect(screen.getAllByText("準備中")).toHaveLength(1);
  });
});
