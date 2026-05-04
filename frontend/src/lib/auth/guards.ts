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
import { cache } from "react";
import { auth } from "@/lib/auth/auth";
import { buildLoginCallbackUrl } from "@/lib/auth/login-redirect-url";
import { narrowRole } from "@/lib/auth/role";
import type { Session } from "@/lib/auth/session";

/**
 * 現在の Better Auth session を取得する。
 *
 * `React.cache` で wrap することで、同一 React Request scope で複数回
 * 呼ばれても backend (Postgres) への問い合わせは 1 回に集約される。
 * `requireSession` / `requireSessionForAction` / `requireAdminForAction` /
 * `typedServer` の `authMiddleware` / `getWatchlistIds` 等から共通で
 * 呼ばれるため、wrap なしだと 1 リクエストで 4-5 回 DB hit していた。
 *
 * cross-request leak は構造的に発生しない (`cache` は per-Request scope)。
 */
export const getCurrentSession = cache(async (): Promise<Session | null> => {
  return auth.api.getSession({
    headers: await headers(),
  });
});

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
 * Server Action 用: 未認証なら `/auth/login?callbackUrl=...` に redirect する。
 *
 * Next.js の `redirect()` は `NEXT_REDIRECT` 特殊 throw で client に伝搬し、
 * browser が自動的に navigate する (caller の `try/catch` は通過するだけで
 * catch block は実行されない)。これにより session 切れた状態でボタン連打
 * しても toast 連発で詰まらず、自然にログインフローへ誘導される。
 *
 * referer から callbackUrl を組み立てる純粋ロジックは
 * `lib/auth/login-redirect-url.ts::buildLoginCallbackUrl` に切り出してある。
 */
export async function requireSessionForAction(): Promise<Session> {
  const session = await getCurrentSession();
  if (!session) {
    const reqHeaders = await headers();
    redirect(buildLoginCallbackUrl(reqHeaders.get("referer")));
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
