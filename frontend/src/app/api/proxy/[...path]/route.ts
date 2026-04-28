import { type NextRequest, NextResponse } from "next/server";
import {
  buildInternalAuthHeaders,
  INTERNAL_API_URL,
  requireEnv,
} from "@/lib/api/internal-config";
import { getCurrentSession } from "@/lib/auth/guards";

// CSRF: state を変更するリクエストは同一オリジンからのみ受け付ける。
// Better Auth の SameSite=Lax cookie に加えた多層防御。
const STATE_CHANGING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

const TRUSTED_ORIGIN = new URL(requireEnv("BETTER_AUTH_URL")).origin;

// Path traversal / SSRF 対策: backend へ転送する path セグメントは
// allowlist (英数字・アンダースコア・ハイフン・ドット・チルダ) のみ許可。
// `..` 単独セグメントは別途明示的に拒否し、URL 解釈ライブラリ依存の挙動差を避ける。
const PATH_SEGMENT_PATTERN = /^[A-Za-z0-9._\-~]+$/;

// backend からブラウザへ素通しさせるレスポンスヘッダの allowlist。
// これ以外は捨てる:
//   - Set-Cookie: backend session を BFF 越しに漏らすとセッション偽装の温床
//   - Authorization / X-Internal-*: 内部 JWT 等の漏洩防止
//   - Server / X-Powered-By: backend のフィンガープリンティング防止
//   - Access-Control-*: CORS は Next.js 側で完結させる (backend のヘッダは混入させない)
//   - Content-Encoding / Content-Length: fetch が自動で decode/再計算するため転送すると二重処理になる
const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "cache-control",
  "etag",
  "last-modified",
  "vary",
  "content-language",
] as const;

function isOriginAllowed(request: NextRequest): boolean {
  const origin = request.headers.get("Origin");
  if (!origin) {
    // Browser の fetch / XHR は Origin を必ず付ける。
    // 欠落していたら CSRF か非ブラウザ経路と判断して拒否。
    return false;
  }
  try {
    return new URL(origin).origin === TRUSTED_ORIGIN;
  } catch {
    return false;
  }
}

function isPathSegmentSafe(segment: string): boolean {
  return segment !== ".." && PATH_SEGMENT_PATTERN.test(segment);
}

async function proxyRequest(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  if (STATE_CHANGING_METHODS.has(request.method) && !isOriginAllowed(request)) {
    return NextResponse.json(
      { detail: "Origin check failed" },
      { status: 403 },
    );
  }

  const session = await getCurrentSession();
  if (!session) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }

  const { path } = await context.params;
  if (!path.every(isPathSegmentSafe)) {
    return NextResponse.json({ detail: "Invalid path" }, { status: 400 });
  }

  const target = `${INTERNAL_API_URL}/${path.join("/")}${request.nextUrl.search}`;

  const proxyHeaders = new Headers();
  proxyHeaders.set(
    "Content-Type",
    request.headers.get("Content-Type") ?? "application/json",
  );
  for (const [name, value] of Object.entries(
    await buildInternalAuthHeaders(session),
  )) {
    proxyHeaders.set(name, value);
  }

  const hasBody = !["GET", "HEAD"].includes(request.method);

  const res = await fetch(target, {
    method: request.method,
    headers: proxyHeaders,
    body: hasBody ? await request.arrayBuffer() : undefined,
  });

  const responseHeaders = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = res.headers.get(name);
    if (value !== null) {
      responseHeaders.set(name, value);
    }
  }
  if (!responseHeaders.has("content-type")) {
    responseHeaders.set("content-type", "application/json");
  }

  return new NextResponse(res.body, {
    status: res.status,
    statusText: res.statusText,
    headers: responseHeaders,
  });
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const PATCH = proxyRequest;
export const DELETE = proxyRequest;
