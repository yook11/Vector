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
