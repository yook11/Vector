import { describe, expect, it } from "vitest";
import { getCategoryKicker } from "./paper-style";

describe("getCategoryKicker", () => {
  // 12 slug 全件の code/hue を固定。trends の独自辞書を撤去した分のカバレッジをここで担保する。
  const CATEGORY_TABLE: [slug: string, code: string, hue: string][] = [
    ["ai", "A.I.", "#0E9E97"],
    ["bio", "BIO", "#6E8B3D"],
    ["computing", "COMPUTE", "#7A5BA8"],
    ["energy", "ENERGY", "#B5752E"],
    ["materials", "MATERIALS", "#6E5A8C"],
    ["mobility", "MOBILITY", "#3F84C0"],
    ["network", "NETWORK", "#2F8F6B"],
    ["other", "MARKET", "#B0852A"],
    ["robotics", "ROBOTICS", "#8A6A4F"],
    ["security", "SECURITY", "#C2562F"],
    ["semiconductor", "SEMICON", "#C04D6E"],
    ["space", "SPACE", "#5B6AB0"],
  ];

  it.each(
    CATEGORY_TABLE,
  )("maps %s to code %s and hue %s", (slug, code, hue) => {
    const kicker = getCategoryKicker(slug);
    expect(kicker.code).toBe(code);
    expect(kicker.hue).toBe(hue);
  });

  it("falls back to NEWS code and hue for an unknown slug", () => {
    const kicker = getCategoryKicker("unknown-slug");
    expect(kicker.code).toBe("NEWS");
    expect(kicker.hue).toBe("#0E9E97");
  });

  it("derives a lighter dark-mode hue", () => {
    const { hue, hueDark } = getCategoryKicker("ai");
    expect(hueDark).not.toBe(hue);
    const channelSum = (hex: string) =>
      Number.parseInt(hex.slice(1, 3), 16) +
      Number.parseInt(hex.slice(3, 5), 16) +
      Number.parseInt(hex.slice(5, 7), 16);
    expect(channelSum(hueDark)).toBeGreaterThan(channelSum(hue));
  });
});
