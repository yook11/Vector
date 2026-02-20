import { NextRequest, NextResponse } from "next/server";

import type { KeywordCreate, KeywordListResponse } from "@/types";

import { create, findByKeyword, getAll } from "../_data/keywords";

export async function GET() {
  const response: KeywordListResponse = { items: getAll() };
  return NextResponse.json(response);
}

export async function POST(request: NextRequest) {
  const body = (await request.json()) as KeywordCreate;

  if (!body.keyword || body.keyword.trim() === "") {
    return NextResponse.json(
      { detail: "Keyword is required" },
      { status: 400 },
    );
  }

  if (findByKeyword(body.keyword)) {
    return NextResponse.json(
      { detail: "Keyword already exists" },
      { status: 409 },
    );
  }

  const created = create(body.keyword.trim(), body.category ?? "custom");
  return NextResponse.json(created, { status: 201 });
}
