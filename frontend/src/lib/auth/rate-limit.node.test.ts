import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { calculateLimit, parseLimit } from "./rate-limit";

describe("parseLimit", () => {
  it("returns fallback when raw is undefined", () => {
    expect(parseLimit(undefined, 120)).toBe(120);
  });

  it("returns fallback when raw is empty string", () => {
    expect(parseLimit("", 120)).toBe(120);
  });

  it("returns fallback for non-numeric", () => {
    expect(parseLimit("abc", 60)).toBe(60);
  });

  it("returns fallback for zero", () => {
    expect(parseLimit("0", 60)).toBe(60);
  });

  it("returns fallback for negative", () => {
    expect(parseLimit("-5", 60)).toBe(60);
  });

  it("parses positive integer", () => {
    expect(parseLimit("250", 60)).toBe(250);
  });

  it("parses leading-integer string", () => {
    // Number.parseInt が前方一致で読むため、これは意図的な許容
    expect(parseLimit("100abc", 60)).toBe(100);
  });
});

describe("calculateLimit", () => {
  it("returns 120 for auth by default", () => {
    expect(calculateLimit("auth", {})).toBe(120);
  });

  it("returns 60 for anon by default", () => {
    expect(calculateLimit("anon", {})).toBe(60);
  });

  it("respects RATE_LIMIT_AUTHED_PER_MIN override", () => {
    expect(calculateLimit("auth", { RATE_LIMIT_AUTHED_PER_MIN: "300" })).toBe(
      300,
    );
  });

  it("respects RATE_LIMIT_ANON_PER_MIN override", () => {
    expect(calculateLimit("anon", { RATE_LIMIT_ANON_PER_MIN: "30" })).toBe(30);
  });

  it("falls back to default for invalid auth override", () => {
    expect(
      calculateLimit("auth", { RATE_LIMIT_AUTHED_PER_MIN: "not-a-number" }),
    ).toBe(120);
  });

  it("falls back to default for invalid anon override", () => {
    expect(calculateLimit("anon", { RATE_LIMIT_ANON_PER_MIN: "0" })).toBe(60);
  });
});
