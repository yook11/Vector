/**
 * Server-only authentication / authorization guards.
 *
 * Route segment の layout だけに認可を載せると、同じ segment 配下に
 * Server Action や Route Handler を後から足したときに layout が実行されず
 * 素通りする。各 mutation / handler が **明示的に** ガードを呼ぶことで、
 * 「layout を 1 つ外したら穴が空く」構造的な脆さを排除する。
 *
 * 使い分け:
 * - Server Component / Layout: `requireSession()` / `requireAdmin()`
 *   未認証/権限不足時は `redirect()` で UX フローへ。
 * - Server Action: `requireSessionForAction()` / `requireAdminForAction()`
 *   未認証は `redirect()` で `/auth/login?callbackUrl=...` に誘導 (toast
 *   無限ループ防止)、admin 権限不足は throw して client catch + toast
 *   (権限不足はログイン誘導しても解決しないため UX を分離)
 */

import "server-only";

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { auth } from "@/lib/auth/auth";
import { narrowRole } from "@/lib/auth/role";
import type { Session } from "@/lib/auth/session";

export async function getCurrentSession(): Promise<Session | null> {
  return auth.api.getSession({
    headers: await headers(),
  });
}

export async function requireSession(): Promise<Session> {
  const session = await getCurrentSession();
  if (!session) {
    redirect("/auth/login");
  }
  return session;
}

export async function requireAdmin(): Promise<Session> {
  const session = await requireSession();
  if (narrowRole(session.user.role) !== "admin") {
    redirect("/");
  }
  return session;
}

/**
 * Server Action から呼ぶ用の login redirect URL を組み立てる。
 *
 * Server Action は browser からの fetch なので Referer header が submit 元
 * page の URL になる。これを callbackUrl として埋め込み、ログイン後に元の
 * page へ戻す。Open redirect 防止として:
 *   - same-origin 以外は捨てる (URL parser に通して例外なら捨てる)
 *   - protocol-relative (`//evil.com`) は捨てる
 *   - `/auth/*` 自体への redirect は callbackUrl 無し (再帰防止)
 */
async function buildLoginRedirectUrl(): Promise<string> {
  const reqHeaders = await headers();
  const referer = reqHeaders.get("referer");
  if (!referer) return "/auth/login";

  let pathname: string;
  let search: string;
  try {
    const url = new URL(referer);
    pathname = url.pathname;
    search = url.search;
  } catch {
    return "/auth/login";
  }

  if (!pathname.startsWith("/") || pathname.startsWith("//")) {
    return "/auth/login";
  }
  if (pathname.startsWith("/auth")) {
    return "/auth/login";
  }
  return `/auth/login?callbackUrl=${encodeURIComponent(pathname + search)}`;
}

/**
 * Server Action 用: 未認証なら `/auth/login?callbackUrl=...` に redirect する。
 *
 * Next.js の `redirect()` は `NEXT_REDIRECT` 特殊 throw で client に伝搬し、
 * browser が自動的に navigate する (caller の `try/catch` は通過するだけで
 * catch block は実行されない)。これにより session 切れた状態でボタン連打
 * しても toast 連発で詰まらず、自然にログインフローへ誘導される。
 */
export async function requireSessionForAction(): Promise<Session> {
  const session = await getCurrentSession();
  if (!session) {
    redirect(await buildLoginRedirectUrl());
  }
  return session;
}

/**
 * Server Action 用: admin でなければ throw する。
 *
 * 未ログインは `requireSessionForAction()` 側で redirect 済み。本関数で
 * throw するのは「ログイン済みだが admin ではない」403 ケースのみ。
 * 権限不足はログイン誘導しても解決しないので、client catch で toast
 * 「権限がありません」を出す UX が正しい。
 *
 * defense in depth: backend 側でも JWT の role claim を検証しているが、
 * Server Action は proxy.ts のガードを経由しないため frontend 層でも
 * 確実に止める。
 */
export async function requireAdminForAction(): Promise<Session> {
  const session = await requireSessionForAction();
  if (narrowRole(session.user.role) !== "admin") {
    throw new Error("Forbidden");
  }
  return session;
}
