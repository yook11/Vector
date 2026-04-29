import { getSessionCookie } from "better-auth/cookies";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { sanitizeCallbackUrl } from "@/lib/proxy/callback-url";
import {
  buildCspDirectives,
  buildCspHeader,
  generateNonce,
} from "@/lib/proxy/csp";

export async function proxy(request: NextRequest) {
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
  const sessionToken = getSessionCookie(request);
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
  // 静的アセットと API ルートは CSP proxy の対象外
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};
