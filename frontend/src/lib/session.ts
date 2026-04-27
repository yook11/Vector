/**
 * Better Auth セッション型のサーバ側エイリアス。
 *
 * `auth.api.getSession()` の返却型から `null` を除外した型。
 * `additionalFields.role` は Better Auth の型推論で自動的に `user.role: string`
 * として表れるので、`Record<string, unknown>` キャストや独自の SessionLike を
 * 持たずに済む。
 */

import type { auth } from "@/lib/auth";

export type Session = NonNullable<
  Awaited<ReturnType<typeof auth.api.getSession>>
>;
