import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  redirect: vi.fn((href: string): never => {
    throw new Error(`redirect:${href}`);
  }),
  requireSessionForAction: vi.fn(),
  revalidatePath: vi.fn(),
  createResearchResponse: vi.fn(),
  submitResearchQuestionCore: vi.fn(),
}));

vi.mock("next/cache", () => ({ revalidatePath: mocks.revalidatePath }));
vi.mock("next/navigation", () => ({ redirect: mocks.redirect }));
vi.mock("server-only", () => ({}));
vi.mock("@/lib/api/hey-api-interceptors", () => ({}));
vi.mock("@/types/sdk.gen", () => ({
  createResearchResponse: mocks.createResearchResponse,
}));
vi.mock("@/lib/auth/guards", () => ({
  requireSessionForAction: mocks.requireSessionForAction,
}));
vi.mock("./submit-research-question-core", () => ({
  submitResearchQuestionCore: mocks.submitResearchQuestionCore,
}));

import { submitResearchQuestion } from "./submit-research-question";

const QUESTION = " 半導体市場への影響は？ ";
const PARSED_QUESTION = "半導体市場への影響は？";
const THREAD_ID = "00000000-0000-4000-a000-000000000001";
const RUN = {
  threadId: THREAD_ID,
  runId: "00000000-0000-4000-a000-000000000002",
};
const QUOTA_RESULT = {
  kind: "daily-request-limit-exceeded" as const,
  resetAt: "2026-07-21T00:00:00+09:00",
  retryAfterSeconds: 0,
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.requireSessionForAction.mockResolvedValue(undefined);
});

describe("submitResearchQuestion", () => {
  it("利用上限結果はそのまま返し、再検証もリダイレクトもしない", async () => {
    mocks.submitResearchQuestionCore.mockResolvedValue(QUOTA_RESULT);

    await expect(submitResearchQuestion(QUESTION, THREAD_ID)).resolves.toEqual(
      QUOTA_RESULT,
    );

    expect(mocks.requireSessionForAction).toHaveBeenCalledOnce();
    expect(mocks.revalidatePath).not.toHaveBeenCalled();
    expect(mocks.redirect).not.toHaveBeenCalled();
  });

  it("既存threadのaccepted結果では検証済み入力をcoreへ渡して再検証する", async () => {
    const accepted = { kind: "accepted" as const, run: RUN };
    mocks.submitResearchQuestionCore.mockResolvedValue(accepted);

    await expect(submitResearchQuestion(QUESTION, THREAD_ID)).resolves.toEqual(
      accepted,
    );

    expect(mocks.submitResearchQuestionCore).toHaveBeenCalledWith({
      question: PARSED_QUESTION,
      threadId: THREAD_ID,
    });
    expect(mocks.revalidatePath).toHaveBeenNthCalledWith(1, "/research");
    expect(mocks.revalidatePath).toHaveBeenNthCalledWith(
      2,
      `/research/${THREAD_ID}`,
    );
    expect(mocks.redirect).not.toHaveBeenCalled();
  });

  it("新規threadのaccepted結果では再検証後にthread画面へリダイレクトする", async () => {
    const accepted = { kind: "accepted" as const, run: RUN };
    mocks.submitResearchQuestionCore.mockResolvedValue(accepted);

    await expect(submitResearchQuestion(QUESTION)).rejects.toThrow(
      `redirect:/research/${THREAD_ID}`,
    );

    expect(mocks.submitResearchQuestionCore).toHaveBeenCalledWith({
      question: PARSED_QUESTION,
    });
    expect(mocks.revalidatePath).toHaveBeenNthCalledWith(1, "/research");
    expect(mocks.revalidatePath).toHaveBeenNthCalledWith(
      2,
      `/research/${THREAD_ID}`,
    );
    expect(mocks.redirect).toHaveBeenCalledWith(`/research/${THREAD_ID}`);
  });
});
