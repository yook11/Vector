import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/error";
import type { ResearchRunStartResponse } from "@/types/types.gen";

const mocks = vi.hoisted(() => ({
  createResearchResponse: vi.fn(),
}));

vi.mock("server-only", () => ({}));
vi.mock("@/lib/api/hey-api-interceptors", () => ({}));
vi.mock("@/types/sdk.gen", () => ({
  createResearchResponse: mocks.createResearchResponse,
}));

import {
  type SubmitResearchQuestionResult,
  submitResearchQuestionCore,
} from "./submit-research-question-core";

const QUESTION = "半導体市場への影響は？";
const THREAD_ID = "00000000-0000-4000-a000-000000000001";
const NOW = new Date("2026-07-20T15:00:00.000Z");
const RUN: ResearchRunStartResponse = {
  threadId: THREAD_ID,
  runId: "00000000-0000-4000-a000-000000000002",
};
const LIMIT_BODY = {
  detail: "Daily research request limit exceeded",
  code: "research_daily_request_limit_exceeded",
  limit: 10,
  resetAt: "2026-07-21T00:00:30+09:00",
};

function apiError(
  body: unknown,
  retryAfter: string | null | undefined,
): ApiError {
  const error = Object.assign(new ApiError(429, "opaque backend detail"), {
    body,
  });
  if (retryAfter !== undefined) {
    Object.assign(error, { retryAfter });
  }
  return error;
}

function submit(now = NOW): Promise<SubmitResearchQuestionResult> {
  return submitResearchQuestionCore({
    question: QUESTION,
    threadId: THREAD_ID,
    now,
  });
}

beforeEach(() => {
  mocks.createResearchResponse.mockReset();
});

describe("submitResearchQuestionCore", () => {
  it("generated SDKへexact optionsを渡しaccepted unionを返す", async () => {
    mocks.createResearchResponse.mockResolvedValue({
      data: RUN,
      response: new Response(null, { status: 202 }),
    });

    await expect(submit()).resolves.toEqual({ kind: "accepted", run: RUN });
    expect(mocks.createResearchResponse).toHaveBeenCalledOnce();
    expect(mocks.createResearchResponse).toHaveBeenCalledWith({
      throwOnError: true,
      cache: "no-store",
      body: { question: QUESTION, threadId: THREAD_ID },
    });
  });

  it("typed 429では有効なRetry-After 0をresetAt計算より優先する", async () => {
    mocks.createResearchResponse.mockRejectedValue(apiError(LIMIT_BODY, "0"));

    await expect(submit()).resolves.toEqual({
      kind: "daily-request-limit-exceeded",
      resetAt: LIMIT_BODY.resetAt,
      retryAfterSeconds: 0,
    });
  });

  it("Retry-Afterが欠損していれば注入nowとの差を切り上げる", async () => {
    const body = {
      ...LIMIT_BODY,
      resetAt: "2026-07-21T00:00:01.001+09:00",
    };

    for (const retryAfter of [null, undefined]) {
      mocks.createResearchResponse.mockRejectedValueOnce(
        apiError(body, retryAfter),
      );
      await expect(submit()).resolves.toEqual({
        kind: "daily-request-limit-exceeded",
        resetAt: body.resetAt,
        retryAfterSeconds: 2,
      });
    }
  });

  it("resetAtが注入now以前ならfallback秒数を0へclampする", async () => {
    const body = {
      ...LIMIT_BODY,
      resetAt: "2026-07-20T23:59:59+09:00",
    };
    mocks.createResearchResponse.mockRejectedValue(apiError(body, null));

    await expect(submit()).resolves.toEqual({
      kind: "daily-request-limit-exceeded",
      resetAt: body.resetAt,
      retryAfterSeconds: 0,
    });
  });

  it("generic errorと識別条件を満たさない429は同じinstanceを再throwする", async () => {
    const errors = [
      new Error("network failed"),
      apiError({ ...LIMIT_BODY, code: "unknown_quota" }, null),
      apiError({ ...LIMIT_BODY, limit: 11 }, null),
      apiError({ ...LIMIT_BODY, resetAt: "2026-07-21T00:00:00" }, null),
      apiError({ ...LIMIT_BODY, resetAt: "1Z" }, null),
      apiError({ ...LIMIT_BODY, resetAt: "2026-02-30T00:00:00Z" }, null),
    ];

    for (const error of errors) {
      mocks.createResearchResponse.mockRejectedValueOnce(error);
      await expect(submit()).rejects.toBe(error);
    }
  });

  it("不正なRetry-After文字列は使わずresetAt fallbackへ収束する", async () => {
    const body = {
      ...LIMIT_BODY,
      resetAt: "2026-07-21T00:00:01.001+09:00",
    };
    const invalidRetryAfterValues = [
      "-1",
      "1.5",
      "1e2",
      "",
      "9007199254740992",
    ];

    for (const retryAfter of invalidRetryAfterValues) {
      mocks.createResearchResponse.mockRejectedValueOnce(
        apiError(body, retryAfter),
      );
      await expect(submit()).resolves.toEqual({
        kind: "daily-request-limit-exceeded",
        resetAt: body.resetAt,
        retryAfterSeconds: 2,
      });
    }
  });
});
