import { describe, expect, it } from "vitest";
import {
  DEFAULT_RESEARCH_THREAD_LIMIT,
  nextResearchLimit,
  parseResearchLimit,
} from "./research";

describe("parseResearchLimit", () => {
  it("defaults to 20 when limit is absent or invalid", () => {
    expect(parseResearchLimit({})).toBe(DEFAULT_RESEARCH_THREAD_LIMIT);
    expect(parseResearchLimit({ limit: "abc" })).toBe(
      DEFAULT_RESEARCH_THREAD_LIMIT,
    );
    expect(parseResearchLimit({ limit: "101" })).toBe(
      DEFAULT_RESEARCH_THREAD_LIMIT,
    );
  });

  it("accepts the first valid limit value", () => {
    expect(parseResearchLimit({ limit: "40" })).toBe(40);
    expect(parseResearchLimit({ limit: ["60", "80"] })).toBe(60);
  });
});

describe("nextResearchLimit", () => {
  it("grows by 20 until total or 100 is reached", () => {
    expect(nextResearchLimit(20, 70)).toBe(40);
    expect(nextResearchLimit(80, 90)).toBe(90);
    expect(nextResearchLimit(90, 200)).toBe(100);
    expect(nextResearchLimit(100, 200)).toBeNull();
  });
});
