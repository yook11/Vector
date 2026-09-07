"use client";

import { getNavItems } from "@/components/layout/nav-items";
import { useSession } from "@/lib/auth/auth-client";
import { narrowRole } from "@/lib/auth/role";

/**
 * 共通シェルの nav 項目を client session から導出する。
 *
 * 基本項目は session 未解決でも返るため static shell / 初回 SSR に載る。
 * admin 用 Settings は session の role が admin と確定したときだけ append する
 * (admin 判定前に admin UI を出さない)。nav はあくまで表示で、実ルートは
 * server 側 requireAdmin で保護する。
 */
export function useNavItems() {
  const { data: session } = useSession();
  const role = session?.user?.role;
  const isAdmin = typeof role === "string" && narrowRole(role) === "admin";
  return getNavItems(isAdmin);
}
