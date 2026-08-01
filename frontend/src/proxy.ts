import { getSessionCookie } from "better-auth/cookies";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import {
  calculateLimits,
  checkRateLimit,
  recordClientIpTrustUnconfigured,
  recordRateLimitSignal,
  recordXffChainObservation,
} from "@/lib/auth/rate-limit";
import { sanitizeCallbackUrl } from "@/lib/proxy/callback-url";
import {
  buildCspDirectives,
  buildCspHeader,
  generateNonce,
} from "@/lib/proxy/csp";
import {
  CLIENT_IP_HEADER,
  countForwardedForValues,
  extractClientIp,
  parseClientIpTrust,
} from "@/lib/proxy/identifier";
import {
  buildRateLimitPlan,
  isAgentRunSseRoute,
  isHealthCheckerUserAgent,
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
  // production の trusted source は CLIENT_IP_TRUST が宣言する (Fly は fly-client-ip、
  // ALB は XFF 末尾)。未宣言は fail-closed で IP 未解決とし、read/`_rsc` を fail-open、
  // anon mutation のみ共有 global bucket で最低限縛る。dev/test は fallback を許可する。
  //
  // session token は下段の認証チェックでも再利用するため、ここで一度だけ取得する。
  // CSP nonce 生成や session 検証より前に実行する。
  // Redis 不通・tiers 空時は fail-open し、storage 障害がアプリ全体の停止に直結しない。
  const sessionToken = getSessionCookie(request);
  const isProduction = process.env.NODE_ENV === "production";
  const rawTrust = process.env.CLIENT_IP_TRUST;
  const trust = parseClientIpTrust(rawTrust);
  if (isProduction && trust === null) {
    recordClientIpTrustUnconfigured(rawTrust ? "invalid" : "unset");
  }
  const forwardedFor = request.headers.get("x-forwarded-for");
  const clientIp = extractClientIp({
    trust,
    flyClientIp: request.headers.get("fly-client-ip"),
    forwardedFor,
    realIp: request.headers.get("x-real-ip"),
    isProduction,
  });
  // ALB は append 固定で必ず 1 値以上を付けるため、XFF の有無が「ALB 経由か」と一致する。
  // health check と service connect の内部呼び出しは XFF を持たず、分母から自然に落ちる
  // (偽装可能な UA 判定に依存しない)。Fly は XFF 末尾がアプリ自身の IP で別構造のため測らない。
  if (isProduction && trust === "alb-xff-last") {
    const forwardedForValues = countForwardedForValues(forwardedFor);
    if (forwardedForValues > 0) {
      recordXffChainObservation(forwardedForValues >= 2);
    }
  }
  if (!isAgentRunSseRoute(pathname)) {
    const plan = buildRateLimitPlan({
      method: request.method,
      hasRsc: request.nextUrl.searchParams.has("_rsc"),
      clientIp,
      sessionToken,
      isProduction,
      limits: calculateLimits(),
    });
    // health check は XFF 無しの正常経路なので missing_ip を恒常ノイズ化させない。
    // 抑制は missing_ip 限定: UA は client が偽装できるため、anon mutation flood の
    // 唯一の兆候である unknown_write まで消せてしまう。
    const isSuppressedSignal =
      plan.signal === "missing_ip" &&
      isHealthCheckerUserAgent(request.headers.get("user-agent"));
    if (plan.signal && !isSuppressedSignal) {
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
  // 解決済み client IP を下流 (route handler / Better Auth) へ渡す唯一の経路。
  // 外来の同名ヘッダは必ず削除し、偽装値が下流に到達しない不変条件を保つ。
  requestHeaders.delete(CLIENT_IP_HEADER);
  if (clientIp !== null) {
    requestHeaders.set(CLIENT_IP_HEADER, clientIp);
  }

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
