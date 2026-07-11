import { getSessionCookie } from "better-auth/cookies";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import {
  calculateLimits,
  checkRateLimit,
  recordRateLimitSignal,
} from "@/lib/auth/rate-limit";
import { sanitizeCallbackUrl } from "@/lib/proxy/callback-url";
import {
  buildCspDirectives,
  buildCspHeader,
  generateNonce,
} from "@/lib/proxy/csp";
import {
  buildRateLimitPlan,
  isAgentRunSseRoute,
} from "@/lib/proxy/rate-limit-plan";

// Next.js 16 の proxy は Node.js runtime 固定。`export const runtime` は使えない。

export async function proxy(request: NextRequest) {
  const pathname = request.nextUrl.pathname;
  const isAuthPage = pathname.startsWith("/auth");
  const isApiRoute = pathname.startsWith("/api/");
  // /design-lab/* は本番認証導線外の UI モック領域なので auth gate 対象外。
  const isDesignLab = pathname.startsWith("/design-lab");

  // --- Rate limit (DoS 防御の一次関門) ---
  //
  // Better Auth 内蔵 rate limit は /api/auth/* router 専用のため、
  // proxy 層で全 request に application-level rate limit をかける。
  //
  // request を class (rsc / read / mutation) × identity (session / IP) で分類し、
  // 該当する全 tier を満たせば通す (ADR-009)。prefetch 由来の `_rsc` GET は寛容な
  // ceiling を別財布で持ち、認証済 request は session sub-bucket + IP ceiling の
  // two-tier-AND で偽造 cookie バイパスを塞ぐ。
  //
  // production は Fly-Client-IP だけを trusted source とし、欠如 (経路異常) 時は
  // read/`_rsc` を fail-open、anon mutation のみ共有 global bucket で最低限縛る。
  // dev/test では XFF/X-Real-IP fallback を許可する。
  //
  // session token は下段の認証チェックでも再利用するため、ここで一度だけ取得する。
  // CSP nonce 生成や session 検証より前に実行する。
  // Redis 不通・tiers 空時は fail-open し、storage 障害がアプリ全体の停止に直結しない。
  const sessionToken = getSessionCookie(request);
  if (!isAgentRunSseRoute(pathname)) {
    const plan = buildRateLimitPlan({
      method: request.method,
      hasRsc: request.nextUrl.searchParams.has("_rsc"),
      flyClientIp: request.headers.get("fly-client-ip"),
      forwardedFor: request.headers.get("x-forwarded-for"),
      realIp: request.headers.get("x-real-ip"),
      sessionToken,
      isProduction: process.env.NODE_ENV === "production",
      limits: calculateLimits(),
    });
    if (plan.signal) {
      recordRateLimitSignal(plan.signal);
    }
    const decision = await checkRateLimit(plan, {
      requestClass: ["GET", "HEAD", "OPTIONS"].includes(request.method)
        ? "read"
        : "mutation",
    });
    if (!decision.allowed) {
      return new NextResponse("Too Many Requests", {
        status: 429,
        headers: {
          "Retry-After": String(decision.retryAfterSeconds),
          "Content-Type": "text/plain; charset=utf-8",
        },
      });
    }
  }

  // --- XSS対策: Content Security Policy (CSP) ---
  // request ごとに nonce を生成し、nonce 付き script のみ実行を許可する。
  // XSS が入り込んだ場合の最終防衛線として、ブラウザ側で実行を制限する。
  const nonce = generateNonce();
  const cspHeader = buildCspHeader(
    buildCspDirectives(nonce, process.env.NODE_ENV === "development"),
  );

  // リクエストヘッダーに nonce を埋め込み、Server Component から読み取れるようにする。
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", cspHeader);

  const response = NextResponse.next({
    request: { headers: requestHeaders },
  });

  response.headers.set("Content-Security-Policy", cspHeader);

  // --- Better Auth 認証チェック ---
  // Cookie 名は Better Auth の getSessionCookie に任せ、proxy 側で
  // dev/prod の cookie 名をハードコードしない。token は rate-limit で取得済みを再利用。
  // /api/* は redirect せず、各 route handler の認証/認可レスポンスに任せる。
  if (!sessionToken && !isAuthPage && !isApiRoute && !isDesignLab) {
    const signInUrl = new URL("/auth/login", request.url);
    // Open redirect 対策: protocol-relative URL や絶対 URL を callbackUrl に入れない。
    const callbackUrl = sanitizeCallbackUrl(pathname);
    if (callbackUrl) {
      signInUrl.searchParams.set("callbackUrl", callbackUrl);
    }
    return NextResponse.redirect(signInUrl);
  }

  return response;
}

export const config = {
  // 静的アセットのみ proxy 対象外。`/api/*` は rate-limit を通し、
  // route handler は `NextResponse.next()` で透過する。
  // App Router では `_next/data` を生成しないため除外しない。
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
