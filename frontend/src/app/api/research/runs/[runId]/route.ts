import "server-only";

import { NextResponse } from "next/server";
import { getResearchRun, ResearchUuidSchema } from "@/features/research";
import { ApiError } from "@/lib/api/error";

const NO_STORE_HEADERS = { "Cache-Control": "no-store" } as const;

interface ResearchRunRouteContext {
  params: Promise<{ runId: string }>;
}

export async function GET(
  _request: Request,
  { params }: ResearchRunRouteContext,
) {
  const { runId } = await params;
  const parsed = ResearchUuidSchema.safeParse(runId);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "Bad Request" },
      { status: 400, headers: NO_STORE_HEADERS },
    );
  }

  try {
    const data = await getResearchRun(parsed.data);
    return NextResponse.json(data, { headers: NO_STORE_HEADERS });
  } catch (err) {
    if (err instanceof ApiError && [401, 403, 404].includes(err.status)) {
      return NextResponse.json(
        { error: err.detail },
        { status: err.status, headers: NO_STORE_HEADERS },
      );
    }
    throw err;
  }
}
