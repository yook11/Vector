import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { getToken } from "next-auth/jwt";

export async function middleware(request: NextRequest) {
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
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");

  const cspDirectives = [
    // デフォルトで全リソースを自身のオリジンに制限
    "default-src 'self'",
    // スクリプト: nonce 付きのみ許可
    // 'strict-dynamic' により、nonce 付きスクリプトが読み込む子スクリプトも
    // 自動的に信頼される（Next.js のコード分割チャンク読み込みに必要）
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${process.env.NODE_ENV === "development" ? " 'unsafe-eval'" : ""}`,
    // スタイル: 自身 + unsafe-inline（Tailwind CSS の動的クラス挿入に必要）
    // TODO: 将来的に nonce ベースへ移行を検討
    "style-src 'self' 'unsafe-inline'",
    // 画像: 自身 + data URI（アイコン等）
    "img-src 'self' data:",
    // フォント: 自身のみ（next/font でセルフホスト済み）
    "font-src 'self'",
    // API 接続先: 自身 + バックエンド API
    `connect-src 'self' ${process.env.NEXT_PUBLIC_API_URL || ""}`,
    // フレーム埋め込み禁止（クリックジャッキング対策、X-Frame-Options: DENY と同等）
    "frame-ancestors 'none'",
    // form の送信先を自身に制限
    "form-action 'self'",
    // base タグの href を自身に制限（base タグインジェクション対策）
    "base-uri 'self'",
  ];

  const cspHeader = cspDirectives.join("; ");

  // リクエストヘッダーに nonce を埋め込み、Server Component から読み取れるようにする
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", cspHeader);

  const response = NextResponse.next({
    request: { headers: requestHeaders },
  });

  response.headers.set("Content-Security-Policy", cspHeader);

  // --- NextAuth 認証チェック ---
  //
  // next-auth/middleware の default export を使わず、getToken() で手動チェック。
  // CSP ヘッダー設定と認証ロジックを単一の middleware に統合するため。
  const token = await getToken({ req: request });
  const isAuthPage = request.nextUrl.pathname.startsWith("/auth");

  if (!token && !isAuthPage) {
    const signInUrl = new URL("/auth/login", request.url);
    signInUrl.searchParams.set("callbackUrl", request.nextUrl.pathname);
    return NextResponse.redirect(signInUrl);
  }

  return response;
}

export const config = {
  // 静的アセットと API ルートは CSP middleware の対象外
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};
