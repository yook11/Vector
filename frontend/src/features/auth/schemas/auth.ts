import { z } from "zod";

export const LoginSchema = z.object({
  // `.email()` は format check が最優先で走るため、`.trim()` を後置すると
  // 周辺空白で fail する。`preprocess` で先に trim を当てる (zod 4 idiom)。
  email: z.preprocess(
    (v) => (typeof v === "string" ? v.trim() : v),
    z.email("有効なメールアドレスを入力してください。"),
  ),
  password: z.string().min(1, "パスワードを入力してください。"),
});
