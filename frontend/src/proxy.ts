import { getSessionCookie } from "better-auth/cookies";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { checkRateLimit } from "@/lib/auth/rate-limit";
import { sanitizeCallbackUrl } from "@/lib/proxy/callback-url";
import {
  buildCspDirectives,
  buildCspHeader,
  generateNonce,
} from "@/lib/proxy/csp";
import { buildIdentifier } from "@/lib/proxy/identifier";

// Next.js 16 の proxy は Node.js runtime に固定 (公式: middleware-to-proxy)。
// `export const runtime` は禁止されており、node-redis / node:crypto は素で使える。

export async function proxy(request: NextRequest) {
  const pathname = request.nextUrl.pathname;
  const isAuthPage = pathname.startsWith("/auth");
  const isApiRoute = pathname.startsWith("/api/");

  // --- Rate limit (DoS 防御の一次関門 / red-team C1 / F17 対策) ---
  //
  // Better Auth 内蔵の rate limit は HTTP router (/api/auth/*) にしか効かず、
  // Vector が依拠する `auth.api.getSession({ headers })` 直呼び経路には
  // 完全にバイパスされる。proxy 層で application-level rate limit を被せて、
  // 認証済 cookie を保持した攻撃者による DB Pool 飽和 DoS を構造的に bound する。
  //
  // identifier は IP-based に統一 (red-team C1 / F2-F4 対策)。cookie 値で
  // bucket を分けると「任意 cookie で別 bucket」「auth bucket への 2x 昇格」
  // 両方の bypass 経路が開くため、proxy 層では per-IP の上限のみを課す。
  // 認証状態に応じた緩和は Better Auth 内蔵 rate-limit (PR-A3) に任せる。
  //
  // 信頼境界 (PR10 / S-AUTH / C1 / F2-F4 構造防御):
  //   - production runtime は Fly.io edge proxy を必ず経由するため、
  //     `Fly-Client-IP` が真の client IP に上書き付与される (incoming 値は
  //     edge で破棄される)。client から偽装不能な唯一の trusted source。
  //   - production で `Fly-Client-IP` 欠如 = Fly proxy bypass の異常経路。
  //     identifier は fail-closed で "unknown" bucket に集約される (詐称された
  //     `x-forwarded-for` を per-IP rate limit に使わない構造保証)。
  //   - dev / test (docker-compose / npm run dev) は `Fly-Client-IP` が無いため
  //     `x-forwarded-for` / `x-real-ip` の従来 fallback 経路を維持する。
  //
  // CSP nonce 生成や session 検証より前に走らせる。429 で即 reject すれば
  // CPU を使う処理に攻撃者を到達させない。Redis 不通時は checkRateLimit が
  // 内部でフェイルオープン (allowed: true) を返すため、運用障害が DoS に
  // 直結しないようになっている (storage は fail-open / identifier source は
  // production で fail-closed の対称使い分け、ADR-006 §3 / §4 参照)。
  const identifier = buildIdentifier(
    request.headers.get("fly-client-ip"),
    request.headers.get("x-forwarded-for"),
    request.headers.get("x-real-ip"),
    process.env.NODE_ENV === "production",
  );
  const decision = await checkRateLimit(identifier);
  if (!decision.allowed) {
    return new NextResponse("Too Many Requests", {
      status: 429,
      headers: {
        "Retry-After": String(decision.retryAfterSeconds),
        "Content-Type": "text/plain; charset=utf-8",
      },
    });
  }

  // --- XSS対策: Content Security Policy (CSP) ---
  //
  // CSP はブラウザに「どのリソースの読み込みを許可するか」を指示する HTTP ヘッダー。
  // 徳丸本 4.16.4 で解説されるように、万が一 XSS 脆弱性が存在しても、
  // CSP が最終防衛線として不正なスクリプト実行をブロックする（多層防御）。
  //
  // nonce（Number Used Once）ベースの CSP を採用:
  //   - リクエストごとに暗号学的に安全な乱数（nonce）を生成
  //   - <script nonce="xxx"> を持つ正規スクリプトのみ実行を許可
  //   - 攻撃者が注入したスクリプトは nonce を知らないため実行されない
  const nonce = generateNonce();
  const cspHeader = buildCspHeader(
    buildCspDirectives(nonce, process.env.NODE_ENV === "development"),
  );

  // リクエストヘッダーに nonce を埋め込み、Server Component から読み取れるようにする
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", cspHeader);

  const response = NextResponse.next({
    request: { headers: requestHeaders },
  });

  response.headers.set("Content-Security-Policy", cspHeader);

  // --- Better Auth 認証チェック ---
  //
  // Better Auth はセッション cookie を使用。cookie 名は環境により切り替わる:
  //   - HTTP (dev): `better-auth.session_token`
  //   - HTTPS (prod): `__Secure-better-auth.session_token`
  // Better Auth の `getSessionCookie` ヘルパーが BETTER_AUTH_URL から
  // useSecureCookies を判定し正しい cookie 名で取得するため、proxy 側で
  // 名前をハードコードしない。実際の検証は BFF proxy 側で行う。
  //
  // /api/* は redirect の対象外:
  //   - /api/auth/* は Better Auth route handler (sign-in/sign-up は anon が
  //     正規に叩く経路、redirect すると login flow が壊れる)
  //   - /api/internal/* は Bearer 認証経路で、anon は 401/403 を route handler
  //     から返すべき
  const sessionToken = getSessionCookie(request);
  if (!sessionToken && !isAuthPage && !isApiRoute) {
    const signInUrl = new URL("/auth/login", request.url);
    // Open redirect 対策: protocol-relative URL (`//evil.com`) や絶対 URL を
    // 埋め込ませない。`request.nextUrl.pathname` は通常 `/...` だが、
    // 将来的に LoginForm が `searchParams.get("callbackUrl")` を読んで
    // `router.push` する実装に発展した場合に備えて構造的に弾いておく。
    const callbackUrl = sanitizeCallbackUrl(pathname);
    if (callbackUrl) {
      signInUrl.searchParams.set("callbackUrl", callbackUrl);
    }
    return NextResponse.redirect(signInUrl);
  }

  return response;
}

export const config = {
  // 静的アセットのみ proxy 対象外。`/api/*` は rate-limit を通すため対象に
  // 含める (red-team C1 / F1 対策)。Better Auth route handler 等は
  // `NextResponse.next()` で透過するので動作影響なし。
  //
  // `_next/data` を除外していない理由: `_next/data/*.json` は Pages Router
  // 専用のデータ取得エンドポイントで、App Router 採用の Vector では生成
  // されない。除外パターンに含めると将来の Next.js が App Router 配下で
  // この path を別用途に流用した際に CSP が抜ける危険があるため、現在は
  // 意図的に matcher に書かない (Next.js 公式 doc の middleware matcher
  // 例も App Router プロジェクトでは `_next/data` を除外していない)。
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
