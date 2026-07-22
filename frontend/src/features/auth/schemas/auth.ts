import { z } from "zod";
import { passwordPolicy } from "@/lib/auth/auth-config";

export const LoginSchema = z.object({
  // `.email()` は format check が最優先で走るため、`.trim()` を後置すると
  // 周辺空白で fail する。`preprocess` で先に trim を当てる (zod 4 idiom)。
  email: z.preprocess(
    (v) => (typeof v === "string" ? v.trim() : v),
    z.email("有効なメールアドレスを入力してください。"),
  ),
  password: z.string().min(1, "パスワードを入力してください。"),
});

const ProvisionUserNameSchema = z
  .string()
  .refine((value) => !/[\r\n]/.test(value), "名前に改行は使えません。")
  .transform((value) => value.trim())
  .pipe(
    z
      .string()
      .min(1, "名前を入力してください。")
      .max(100, "名前は100文字以内で入力してください。")
      .regex(
        /^[\p{L}\p{N}\p{Zs}_-]+$/u,
        "名前は文字、数字、空白、ハイフン、アンダースコアのみ使えます。",
      ),
  );

const ProvisionUserEmailSchema = z
  .string()
  .transform((value) => value.trim())
  .pipe(z.email("有効なメールアドレスを入力してください。"))
  .transform((value) => value.toLowerCase());

export const ProvisionUserSchema = z.strictObject({
  name: ProvisionUserNameSchema,
  email: ProvisionUserEmailSchema,
  password: z
    .string()
    .min(
      passwordPolicy.minLength,
      `パスワードは${passwordPolicy.minLength}文字以上で入力してください。`,
    )
    .max(
      passwordPolicy.maxLength,
      `パスワードは${passwordPolicy.maxLength}文字以内で入力してください。`,
    ),
});
