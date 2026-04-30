import { describe, expect, it } from "vitest";
import { PositiveIdParamSchema, PositiveIdSchema } from "./id";

describe("PositiveIdSchema", () => {
  it.each([
    1,
    42,
    100,
    Number.MAX_SAFE_INTEGER,
  ])("%s は valid (positive integer)", (value) => {
    expect(PositiveIdSchema.safeParse(value).success).toBe(true);
  });

  it.each([
    ["0", 0],
    ["負数", -1],
    ["小数", 1.5],
    ["NaN", Number.NaN],
    ["Infinity", Number.POSITIVE_INFINITY],
  ])("%s は invalid", (_label, value) => {
    expect(PositiveIdSchema.safeParse(value).success).toBe(false);
  });

  it.each([
    ["string", "1"],
    ["null", null],
    ["undefined", undefined],
    ["object", {}],
  ])("%s 型は invalid (coerce しない)", (_label, value) => {
    expect(PositiveIdSchema.safeParse(value).success).toBe(false);
  });
});

describe("PositiveIdParamSchema", () => {
  it.each([
    ["'1'", "1", 1],
    ["'42'", "42", 42],
    ["'100'", "100", 100],
  ])("%s は coerce + valid", (_label, value, expected) => {
    const result = PositiveIdParamSchema.safeParse(value);
    expect(result.success).toBe(true);
    if (result.success) expect(result.data).toBe(expected);
  });

  it.each([
    ["非数字", "abc"],
    ["空文字", ""],
    ["0", "0"],
    ["負数", "-1"],
    ["小数", "1.5"],
  ])("%s は invalid", (_label, value) => {
    expect(PositiveIdParamSchema.safeParse(value).success).toBe(false);
  });
});
