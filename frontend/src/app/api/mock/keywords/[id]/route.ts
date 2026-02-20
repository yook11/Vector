import { NextRequest, NextResponse } from "next/server";

import type { KeywordUpdate } from "@/types";

import { findById, remove, update } from "../../_data/keywords";

export async function PATCH(
  request: NextRequest,
  { params }: { params: { id: string } },
) {
  const id = Number(params.id);

  if (!findById(id)) {
    return NextResponse.json(
      { detail: "Keyword not found" },
      { status: 404 },
    );
  }

  const body = (await request.json()) as KeywordUpdate;
  const updated = update(id, body);

  return NextResponse.json(updated);
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: { id: string } },
) {
  const id = Number(params.id);

  if (!remove(id)) {
    return NextResponse.json(
      { detail: "Keyword not found" },
      { status: 404 },
    );
  }

  return new NextResponse(null, { status: 204 });
}
