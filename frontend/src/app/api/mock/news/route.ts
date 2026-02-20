import { NextRequest, NextResponse } from "next/server";

import type { PaginatedNewsResponse, Sentiment } from "@/types";

import { mockNews } from "../_data/news";

export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;

  let filtered = [...mockNews];

  // Filter by keywordId
  const keywordId = searchParams.get("keywordId");
  if (keywordId) {
    const kid = Number(keywordId);
    filtered = filtered.filter((a) =>
      a.keywords.some((k) => k.id === kid),
    );
  }

  // Filter by sentiment
  const sentiment = searchParams.get("sentiment") as Sentiment | null;
  if (sentiment) {
    filtered = filtered.filter((a) => a.analysis?.sentiment === sentiment);
  }

  // Filter by minImpact
  const minImpact = searchParams.get("minImpact");
  if (minImpact) {
    const min = Number(minImpact);
    filtered = filtered.filter(
      (a) => a.analysis && a.analysis.impactScore >= min,
    );
  }

  // Sort
  const sortBy = searchParams.get("sortBy") ?? "publishedAt";
  const sortOrder = searchParams.get("sortOrder") ?? "desc";
  const multiplier = sortOrder === "asc" ? 1 : -1;

  filtered.sort((a, b) => {
    if (sortBy === "impactScore") {
      const scoreA = a.analysis?.impactScore ?? 0;
      const scoreB = b.analysis?.impactScore ?? 0;
      return (scoreA - scoreB) * multiplier;
    }
    // Default: publishedAt
    const dateA = a.publishedAt ?? "";
    const dateB = b.publishedAt ?? "";
    return dateA.localeCompare(dateB) * multiplier;
  });

  // Paginate
  const page = Math.max(1, Number(searchParams.get("page") ?? 1));
  const perPage = Math.min(100, Math.max(1, Number(searchParams.get("perPage") ?? 20)));
  const total = filtered.length;
  const totalPages = Math.ceil(total / perPage);
  const start = (page - 1) * perPage;
  const items = filtered.slice(start, start + perPage);

  const response: PaginatedNewsResponse = {
    items,
    total,
    page,
    perPage,
    totalPages,
  };

  return NextResponse.json(response);
}
