import "server-only";

import "@/lib/api/hey-api-interceptors";
import { ApiError } from "@/lib/api/error";
import { createResearchResponse as createResearchResponseSdk } from "@/types/sdk.gen";
import type {
  ResearchDailyRequestLimitExceededResponse,
  ResearchRunStartResponse,
} from "@/types/types.gen";

type DailyRequestLimitBody = Pick<
  ResearchDailyRequestLimitExceededResponse,
  "code" | "limit" | "resetAt"
>;

export type SubmitResearchQuestionResult =
  | { kind: "accepted"; run: ResearchRunStartResponse }
  | {
      kind: "daily-request-limit-exceeded";
      resetAt: string;
      retryAfterSeconds: number;
    };

const RESET_AT_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-](\d{2}):(\d{2}))$/;
const RETRY_AFTER_PATTERN = /^\d+$/;

function isLeapYear(year: number): boolean {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
}

function daysInMonth(year: number, month: number): number {
  if (month === 2) return isLeapYear(year) ? 29 : 28;
  if (month === 4 || month === 6 || month === 9 || month === 11) return 30;
  return 31;
}

function isValidResetAt(value: string): boolean {
  const match = RESET_AT_PATTERN.exec(value);
  if (match === null) return false;

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const offsetHour = Number(match[7] ?? 0);
  const offsetMinute = Number(match[8] ?? 0);

  if (year < 1 || month < 1 || month > 12) return false;
  if (day < 1 || day > daysInMonth(year, month)) return false;
  if (hour > 23 || minute > 59 || second > 59) return false;
  if (offsetHour > 23 || offsetMinute > 59) return false;
  return Number.isFinite(Date.parse(value));
}

function isDailyRequestLimitBody(body: unknown): body is DailyRequestLimitBody {
  if (body === null || typeof body !== "object") return false;
  if (
    !("code" in body) ||
    body.code !== "research_daily_request_limit_exceeded"
  ) {
    return false;
  }
  if (!("limit" in body) || body.limit !== 10) return false;
  if (!("resetAt" in body) || typeof body.resetAt !== "string") return false;
  return isValidResetAt(body.resetAt);
}

function validRetryAfterSeconds(retryAfter: string | null): number | null {
  if (retryAfter === null || !RETRY_AFTER_PATTERN.test(retryAfter)) return null;
  const seconds = Number(retryAfter);
  return Number.isSafeInteger(seconds) && seconds >= 0 ? seconds : null;
}

export async function submitResearchQuestionCore({
  question,
  threadId,
  now = new Date(),
}: {
  question: string;
  threadId?: string;
  now?: Date;
}): Promise<SubmitResearchQuestionResult> {
  try {
    const { data } = await createResearchResponseSdk({
      throwOnError: true,
      cache: "no-store",
      body: {
        question,
        ...(threadId !== undefined ? { threadId } : {}),
      },
    });
    return { kind: "accepted", run: data };
  } catch (error) {
    if (
      !(error instanceof ApiError) ||
      error.status !== 429 ||
      !isDailyRequestLimitBody(error.body)
    ) {
      throw error;
    }

    const headerSeconds = validRetryAfterSeconds(error.retryAfter);
    const retryAfterSeconds =
      headerSeconds ??
      Math.max(
        0,
        Math.ceil((Date.parse(error.body.resetAt) - now.getTime()) / 1000),
      );
    return {
      kind: "daily-request-limit-exceeded",
      resetAt: error.body.resetAt,
      retryAfterSeconds,
    };
  }
}
