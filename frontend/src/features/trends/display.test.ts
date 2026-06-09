import { describe, expect, it } from "vitest";
import type { MentionType } from "@/types";
import { MENTION_TYPE_META } from "./display";

describe("MENTION_TYPE_META", () => {
  const ALL_TYPES: MentionType[] = [
    "company",
    "product",
    "technology",
    "person",
    "academic",
    "government",
  ];

  it("6 種類すべてのキーを持つ", () => {
    for (const type of ALL_TYPES) {
      expect(MENTION_TYPE_META).toHaveProperty(type);
    }
  });

  it("各 label が期待値と一致する", () => {
    expect(MENTION_TYPE_META.company.label).toBe("企業");
    expect(MENTION_TYPE_META.product.label).toBe("製品");
    expect(MENTION_TYPE_META.technology.label).toBe("技術");
    expect(MENTION_TYPE_META.person.label).toBe("人物");
    expect(MENTION_TYPE_META.academic.label).toBe("研究");
    expect(MENTION_TYPE_META.government.label).toBe("政府");
  });
});
