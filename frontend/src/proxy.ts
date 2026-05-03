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
  // session cookie は rate limit identifier と auth check の両方で使う。
  // 1 回だけ取得して使い回す (getSessionCookie は cookie 名解決のため毎回コスト)。
  const sessionToken = getSessionCookie(request);

  // --- Rate limit (DoS 防御の一次関門 / red-team C8 / F17 対策) ---
  //
  // Better Auth 内蔵の rate limit は HTTP router (/api/auth/*) のみに効き、
  // Vector が依拠する `auth.api.getSession({ headers })` 直呼び経路には
  // 完全にバイパスされる。proxy 層で application-level rate limit を被せて、
  // 認証済 cookie を保持した攻撃者による DB Pool 飽和 DoS を構造的に bound する。
  //
  // CSP nonce 生成や session 検証より前に走らせる。429 で即 reject すれば
  // CPU を使う処理に攻撃者を到達させない。Redis 不通時は checkRateLimit が
  // 内部でフェイルオープン (allowed: true) を返すため、運用障害が DoS に
  // 直結しないようになっている。
  const identifier = buildIdentifier(
    sessionToken ?? null,
    request.headers.get("x-forwarded-for"),
    request.headers.get("x-real-ip"),
  );
  if (identifier) {
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
  const isAuthPage = request.nextUrl.pathname.startsWith("/auth");

  if (!sessionToken && !isAuthPage) {
    const signInUrl = new URL("/auth/login", request.url);
    // Open redirect 対策: protocol-relative URL (`//evil.com`) や絶対 URL を
    // 埋め込ませない。`request.nextUrl.pathname` は通常 `/...` だが、
    // 将来的に LoginForm が `searchParams.get("callbackUrl")` を読んで
    // `router.push` する実装に発展した場合に備えて構造的に弾いておく。
    const callbackUrl = sanitizeCallbackUrl(request.nextUrl.pathname);
    if (callbackUrl) {
      signInUrl.searchParams.set("callbackUrl", callbackUrl);
    }
    return NextResponse.redirect(signInUrl);
  }

  return response;
}

export const config = {
  // 静的アセットと API ルートは CSP proxy の対象外。
  //
  // `_next/data` を除外していない理由: `_next/data/*.json` は Pages Router
  // 専用のデータ取得エンドポイントで、App Router 採用の Vector では生成
  // されない。除外パターンに含めると将来の Next.js が App Router 配下で
  // この path を別用途に流用した際に CSP が抜ける危険があるため、現在は
  // 意図的に matcher に書かない (Next.js 公式 doc の middleware matcher
  // 例も App Router プロジェクトでは `_next/data` を除外していない)。
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};
