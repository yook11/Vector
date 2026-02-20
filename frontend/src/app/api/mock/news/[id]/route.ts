import { NextRequest, NextResponse } from "next/server";

import { mockNews } from "../../_data/news";

export async function GET(
  _request: NextRequest,
  { params }: { params: { id: string } },
) {
  const id = Number(params.id);
  const article = mockNews.find((a) => a.id === id);

  if (!article) {
    return NextResponse.json(
      { detail: "News article not found" },
      { status: 404 },
    );
  }

  return NextResponse.json(article);
}
