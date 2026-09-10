import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  getArticleListRevision,
  renewArticleListRevision,
} from "./article-list-revision";

beforeEach(() => {
  globalThis.process.vectorArticleListRevision = undefined;
});

describe("article list revision", () => {
  it("初回に生成した不透明な値を同じprocessで共有する", () => {
    const revision = getArticleListRevision();

    expect(revision).toMatch(/^[0-9a-f-]{36}$/);
    expect(getArticleListRevision()).toBe(revision);
  });

  it("通知時に新しい値へ変更する", () => {
    const initialRevision = getArticleListRevision();
    const renewedRevision = renewArticleListRevision();

    expect(renewedRevision).not.toBe(initialRevision);
    expect(getArticleListRevision()).toBe(renewedRevision);
  });
});
