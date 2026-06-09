import { describe, expect, it } from "vitest";
import {
  DEFAULT_WINDOW,
  hoursToWindow,
  isWindowOption,
  resolveWindow,
  WINDOW_OPTIONS,
  windowToHours,
} from "./window";

describe("resolveWindow", () => {
  it.each(WINDOW_OPTIONS)("有効な label '%s' をそのまま返す", (opt) => {
    expect(resolveWindow(opt)).toBe(opt);
  });

  it("未指定は default (24h) に落とす", () => {
    expect(resolveWindow(undefined)).toBe(DEFAULT_WINDOW);
    expect(DEFAULT_WINDOW).toBe("24h");
  });

  it("label でない不正値は default に落とす", () => {
    expect(resolveWindow("invalid")).toBe("24h");
  });

  it("API 数値表記 (168) は label でないので default に落とす", () => {
    expect(resolveWindow("168")).toBe("24h");
  });

  it("配列値は default に落とす", () => {
    expect(resolveWindow(["24h", "48h"])).toBe("24h");
  });
});

describe("windowToHours", () => {
  it.each([
    ["24h", 24],
    ["48h", 48],
    ["72h", 72],
    ["7d", 168],
  ] as const)("'%s' を %i に変換する", (label, hours) => {
    expect(windowToHours(label)).toBe(hours);
  });
});

describe("hoursToWindow", () => {
  it.each([
    [24, "24h"],
    [48, "48h"],
    [72, "72h"],
    [168, "7d"],
  ] as const)("windowHours %i を label '%s' に戻す", (hours, label) => {
    expect(hoursToWindow(hours)).toBe(label);
  });

  it("windowToHours と往復で一致する (168 は 7d)", () => {
    for (const opt of WINDOW_OPTIONS) {
      expect(hoursToWindow(windowToHours(opt))).toBe(opt);
    }
  });
});

describe("isWindowOption", () => {
  it("許可 label は true", () => {
    expect(isWindowOption("7d")).toBe(true);
  });

  it("非許可値は false", () => {
    expect(isWindowOption("7days")).toBe(false);
    expect(isWindowOption("168")).toBe(false);
  });
});
