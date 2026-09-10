import "server-only";

import { connection, NextResponse } from "next/server";
import { getArticleListRevision } from "@/lib/cache/article-list-revision";

export async function GET() {
  await connection();
  return NextResponse.json(
    { revision: getArticleListRevision() },
    { headers: { "Cache-Control": "no-store" } },
  );
}
