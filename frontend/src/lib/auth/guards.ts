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
  if (session.user.role !== "admin") {
    redirect("/");
  }
  return session;
}
