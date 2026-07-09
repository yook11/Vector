import { z } from "zod";
import type { SearchParams } from "@/lib/types/route";

export const DEFAULT_RESEARCH_THREAD_LIMIT = 20;
export const MAX_RESEARCH_THREAD_LIMIT = 100;

export const ResearchQuestionSchema = z.string().trim().min(1).max(1000);

export const ResearchUuidSchema = z.string().uuid();

function firstParam(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

export function parseResearchLimit(searchParams: SearchParams): number {
  const raw = firstParam(searchParams.limit);
  if (raw === undefined) return DEFAULT_RESEARCH_THREAD_LIMIT;
  const parsed = z.coerce
    .number()
    .int()
    .min(1)
    .max(MAX_RESEARCH_THREAD_LIMIT)
    .safeParse(raw);
  return parsed.success ? parsed.data : DEFAULT_RESEARCH_THREAD_LIMIT;
}

export function nextResearchLimit(
  current: number,
  total: number,
): number | null {
  if (current >= total || current >= MAX_RESEARCH_THREAD_LIMIT) return null;
  return Math.min(
    current + DEFAULT_RESEARCH_THREAD_LIMIT,
    total,
    MAX_RESEARCH_THREAD_LIMIT,
  );
}
