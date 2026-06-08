import { describe, expect, it } from "vitest";
import { formatGrowthRate } from "./percent";

describe("formatGrowthRate", () => {
  describe("正の値", () => {
    it("0.42 → +42%", () => {
      expect(formatGrowthRate(0.42)).toBe("+42%");
    });

    it("5.0 → +500%", () => {
      expect(formatGrowthRate(5.0)).toBe("+500%");
    });
  });

  describe("負の値", () => {
    it("-0.07 → U+2212 付き 7%(ハイフンでない)", () => {
      const result = formatGrowthRate(-0.07);
      // U+2212 MINUS SIGN を使う(ASCII ハイフン "-" と区別)
      expect(result).toBe("−7%");
      expect(result).not.toBe("-7%");
    });

    it("-1 → −100%", () => {
      expect(formatGrowthRate(-1)).toBe("−100%");
    });
  });

  describe("ゼロ", () => {
    it("0 → +0%", () => {
      expect(formatGrowthRate(0)).toBe("+0%");
    });
  });

  describe("丸め", () => {
    it("0.126 → +13%(Math.round)", () => {
      expect(formatGrowthRate(0.126)).toBe("+13%");
    });

    it("-0.124 → −12%(絶対値を丸めてから符号を付ける)", () => {
      expect(formatGrowthRate(-0.124)).toBe("−12%");
    });
  });
});
