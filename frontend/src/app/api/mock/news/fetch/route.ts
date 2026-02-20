import { NextResponse } from "next/server";

import type { NewsFetchResponse } from "@/types";

import { getAll } from "../../_data/keywords";

export async function POST() {
  const activeKeywords = getAll().filter((k) => k.isActive);

  const response: NewsFetchResponse = {
    message: "Fetch started",
    keywordsCount: activeKeywords.length,
    jobId: `fetch-${Date.now()}`,
  };

  return NextResponse.json(response);
}
