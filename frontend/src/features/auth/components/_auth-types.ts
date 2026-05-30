/**
 * Better Auth client の戻り値型から error 型を派生させる。
 *
 * `signIn.email` / `signUp.email` の戻り値から `["error"]` を抽出し、
 * 上流仕様変更をコンパイル時に検知する。
 */

import type { signIn, signUp } from "@/lib/auth/auth-client";

export type SignInError = NonNullable<
  Awaited<ReturnType<typeof signIn.email>>["error"]
>;

export type SignUpError = NonNullable<
  Awaited<ReturnType<typeof signUp.email>>["error"]
>;
