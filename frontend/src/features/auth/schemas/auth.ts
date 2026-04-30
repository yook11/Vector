import { z } from "zod";

export const LoginSchema = z.object({
  // `.email()` は format check が最優先で走るため、`.trim()` を後置すると
  // 周辺空白で fail する。`preprocess` で先に trim を当てる (zod 4 idiom)。
  email: z.preprocess(
    (v) => (typeof v === "string" ? v.trim() : v),
    z.email("Please enter a valid email address"),
  ),
  password: z.string().min(1, "Password is required"),
});

// HTML5 input pattern は `u` flag を持たず `\w` が ASCII のみ評価されるため、
// 旧 RegisterForm.tsx の `pattern="[\w\s\-]+"` は日本語 displayName を弾いていた
// (i18n bug)。検証本体を zod 側 (Unicode property `\p{L}` `\p{N}`) に移し、
// HTML5 pattern 属性は撤去する。
const DISPLAY_NAME_PATTERN = /^[\p{L}\p{N}_ -]+$/u;

// 空文字 ("") を undefined に正規化したい一方、`.regex(...)` は空文字を fail させる。
// 順序問題を回避するため `union([validString, literal("")])` で先に空文字を吸収し、
// `.optional()` で undefined も許容する 3 状態 (string / "" / undefined) を表現する。
const DisplayNameSchema = z
  .union([
    z
      .string()
      .trim()
      .min(1)
      .max(100, "Display name must be at most 100 characters")
      .regex(
        DISPLAY_NAME_PATTERN,
        "Display name can only contain letters, numbers, spaces, hyphens, and underscores",
      ),
    z.literal("").transform(() => undefined),
  ])
  .optional();

export const RegisterSchema = z.object({
  // `.email()` は format check が最優先で走るため、`.trim()` を後置すると
  // 周辺空白で fail する。`preprocess` で先に trim を当てる (zod 4 idiom)。
  email: z.preprocess(
    (v) => (typeof v === "string" ? v.trim() : v),
    z.email("Please enter a valid email address"),
  ),
  password: z
    .string()
    .min(8, "Password must be at least 8 characters")
    .max(128, "Password is too long"),
  displayName: DisplayNameSchema,
});
