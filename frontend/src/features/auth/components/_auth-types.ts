/**
 * Better Auth client の戻り値型から error 型を派生させる。
 *
 * 旧 `RegisterForm.tsx` の手書き `AuthErrorLike` interface は Better Auth が
 * 内部実装を変えるとサイレントに drift していた。本ファイルで `signIn.email` /
 * `signUp.email` の戻り値から `["error"]` を抽出することで、上流仕様変更を
 * コンパイラが教える経路に切り替える。
 */

import type { signIn, signUp } from "@/lib/auth/auth-client";

export type SignInError = NonNullable<
  Awaited<ReturnType<typeof signIn.email>>["error"]
>;

export type SignUpError = NonNullable<
  Awaited<ReturnType<typeof signUp.email>>["error"]
>;
