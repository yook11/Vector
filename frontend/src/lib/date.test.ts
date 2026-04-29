import { describe, expect, it } from "vitest";
import { formatDate } from "./date";

describe("formatDate", () => {
  describe("falsy input", () => {
    it("returns 'Unknown' for null", () => {
      expect(formatDate(null)).toBe("Unknown");
    });

    it("returns 'Unknown' for undefined", () => {
      expect(formatDate(undefined)).toBe("Unknown");
    });

    it("returns 'Unknown' for empty string", () => {
      expect(formatDate("")).toBe("Unknown");
    });
  });

  describe("ja-JP formatting (structural assertions)", () => {
    // Intl.DateTimeFormat の出力は ICU バージョンで微妙に揺れるため、
    // 完全一致ではなく構造 (年月日が含まれる / 元日付の数字が含まれる) で検証する。
    it("contains year, month, day markers and the date numbers", () => {
      const result = formatDate("2024-03-15");
      expect(result).toContain("年");
      expect(result).toContain("月");
      expect(result).toContain("日");
      expect(result).toMatch(/2024/);
      expect(result).toMatch(/15/);
    });

    it("does not include time markers when withTime is omitted", () => {
      const result = formatDate("2024-03-15T10:30:00Z");
      expect(result).not.toMatch(/:\d{2}/);
    });
  });

  describe("withTime: true", () => {
    it("includes a hh:mm-shaped time fragment", () => {
      const result = formatDate("2024-03-15T10:30:00Z", { withTime: true });
      expect(result).toContain("年");
      expect(result).toMatch(/\d{2}:\d{2}/);
    });
  });
});
