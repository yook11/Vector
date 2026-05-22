/**
 * 内部 revalidate endpoint。backend (FrontendRevalidateNotifier) からのみ叩かれる。
 *
 * 認証は REVALIDATE_BEARER_SECRET を Bearer 直接 (system-to-system のため JWT TTL は
 * 不要、単一 endpoint でセッションも持たない)。constant-time 比較で timing 攻撃を防ぐ。
 *
 * 失敗の見える化: 401/403/400 はすべて status code + JSON で返す。backend 側は
 * raise しない warn 降格 (`feedback_failure_visibility.md`) なので、frontend log
 * (Next.js server log) と backend log の両方で revalidate ミスを検知できる。
 */

import "server-only";

import { timingSafeEqual } from "node:crypto";
import { updateTag } from "next/cache";
import { type NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { requireEnv } from "@/lib/env";

// backend→frontend revalidate Bearer。BFF JWT 署名鍵とは別 secret に分離
// (red-team C1: 1 secret 漏洩で両境界が陥落するのを防ぐ)。
const REVALIDATE_BEARER_SECRET = requireEnv(
  "REVALIDATE_BEARER_SECRET",
  "generate one with `openssl rand -hex 32`",
);

const SECRET_BYTES = Buffer.from(REVALIDATE_BEARER_SECRET);

const Body = z.object({
  tags: z.array(z.string().min(1)).min(1),
});

function verifyBearer(header: string | null): boolean {
  if (!header?.startsWith("Bearer ")) return false;
  const presented = Buffer.from(header.slice(7));
  // timingSafeEqual は同サイズ Buffer 必須。サイズ不一致は短絡で false。
  if (presented.length !== SECRET_BYTES.length) return false;
  return timingSafeEqual(presented, SECRET_BYTES);
}

export async function POST(req: NextRequest) {
  const auth = req.headers.get("Authorization");
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  if (!verifyBearer(auth)) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }

  const json = await req.json().catch(() => null);
  const parsed = Body.safeParse(json);
  if (!parsed.success) {
    return NextResponse.json({ error: "Bad Request" }, { status: 400 });
  }

  for (const tag of parsed.data.tags) {
    updateTag(tag);
  }
  return NextResponse.json({ ok: true, count: parsed.data.tags.length });
}
