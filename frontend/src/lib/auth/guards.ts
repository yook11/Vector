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
 * - Route Handler / Server Action: `getCurrentSession()` で取得し、
 *   nullable の戻り値を見て自前で 401/403 を返す (redirect は API 文脈に不適)。
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
 * Server Action / Route Handler 用: 認証済みでなければ throw する。
 *
 * `requireSession()` と違い `redirect()` はせず、Error を投げる。
 * Server Action 内で投げると React が呼び出し側 (Client) の `catch` に
 * 配送するので、`useOptimistic` の自動 revert + toast 表示が成立する。
 */
export async function requireSessionForAction(): Promise<Session> {
  const session = await getCurrentSession();
  if (!session) throw new Error("Unauthorized");
  return session;
}

/**
 * Server Action / Route Handler 用: admin でなければ throw する。
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
