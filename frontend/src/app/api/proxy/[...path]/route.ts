import { headers } from "next/headers";
import { type NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_API_URL =
  process.env.INTERNAL_API_URL ?? "http://localhost:8000/api/v1";

// BFF プロキシとバックエンドの共有秘密。
// デフォルト値や `??` フォールバックは持たせない: 未設定時はモジュール
// 読込時に throw して fail-fast にする (build / 起動時に発覚させる)。
// 値は `openssl rand -hex 32` などで生成し `.env` で必ず設定する。
const INTERNAL_SECRET = (() => {
  const value = process.env.INTERNAL_API_SECRET;
  if (!value) {
    throw new Error(
      "INTERNAL_API_SECRET is required; generate one with `openssl rand -hex 32`",
    );
  }
  return value;
})();

async function proxyRequest(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const session = await auth.api.getSession({
    headers: await headers(),
  });

  if (!session) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }

  const { path } = await context.params;
  const target = `${INTERNAL_API_URL}/${path.join("/")}${request.nextUrl.search}`;

  const proxyHeaders = new Headers();
  proxyHeaders.set(
    "Content-Type",
    request.headers.get("Content-Type") ?? "application/json",
  );
  proxyHeaders.set("X-User-ID", session.user.id);
  proxyHeaders.set(
    "X-User-Role",
    ((session.user as Record<string, unknown>).role as string) ?? "user",
  );
  proxyHeaders.set("X-Internal-Secret", INTERNAL_SECRET);

  const hasBody = !["GET", "HEAD"].includes(request.method);

  const res = await fetch(target, {
    method: request.method,
    headers: proxyHeaders,
    body: hasBody ? await request.arrayBuffer() : undefined,
  });

  return new NextResponse(res.body, {
    status: res.status,
    statusText: res.statusText,
    headers: {
      "Content-Type": res.headers.get("Content-Type") ?? "application/json",
    },
  });
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const PATCH = proxyRequest;
export const DELETE = proxyRequest;
