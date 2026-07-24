import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requireSessionForAction: vi.fn(),
  createResearchResponse: vi.fn(),
  submitResearchQuestionCore: vi.fn(),
}));

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
  it("利用上限結果はそのまま返す", async () => {
    mocks.submitResearchQuestionCore.mockResolvedValue(QUOTA_RESULT);

    await expect(submitResearchQuestion(QUESTION, THREAD_ID)).resolves.toEqual(
      QUOTA_RESULT,
    );

    expect(mocks.requireSessionForAction).toHaveBeenCalledOnce();
  });

  it("既存threadのaccepted結果は検証済み入力をcoreへ渡してそのまま返す", async () => {
    const accepted = { kind: "accepted" as const, run: RUN };
    mocks.submitResearchQuestionCore.mockResolvedValue(accepted);

    await expect(submitResearchQuestion(QUESTION, THREAD_ID)).resolves.toEqual(
      accepted,
    );

    expect(mocks.submitResearchQuestionCore).toHaveBeenCalledWith({
      question: PARSED_QUESTION,
      threadId: THREAD_ID,
    });
  });

  it("新規threadのaccepted結果は検証済み入力をcoreへ渡してそのまま返す", async () => {
    const accepted = { kind: "accepted" as const, run: RUN };
    mocks.submitResearchQuestionCore.mockResolvedValue(accepted);

    await expect(submitResearchQuestion(QUESTION)).resolves.toEqual(accepted);

    expect(mocks.submitResearchQuestionCore).toHaveBeenCalledWith({
      question: PARSED_QUESTION,
    });
  });

  it("認証redirectはschema検証とAPI呼び出しより前にそのまま伝播する", async () => {
    const authRedirect = Object.assign(new Error("NEXT_REDIRECT"), {
      digest: "NEXT_REDIRECT;replace;/auth/login;303;",
    });
    mocks.requireSessionForAction.mockRejectedValue(authRedirect);

    await expect(submitResearchQuestion(QUESTION)).rejects.toBe(authRedirect);

    expect(mocks.submitResearchQuestionCore).not.toHaveBeenCalled();
  });

  it("schema違反はAPI呼び出し前にrejectする", async () => {
    await expect(submitResearchQuestion("   ")).rejects.toThrow();

    expect(mocks.submitResearchQuestionCore).not.toHaveBeenCalled();
  });
});
