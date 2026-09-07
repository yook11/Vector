import { z } from "zod";

const ENCODED_PATH_SEPARATOR = /%(?:2f|5c|3f|23)/i;
const PERCENT_ENCODED_BYTE = /%[0-9a-f]{2}/i;
const VALIDATION_ORIGIN = "https://vector.invalid";

export function hasUnsafeUrlCharacters(value: string): boolean {
  return [...value].some(
    (character) =>
      character === "\\" ||
      character.charCodeAt(0) < 32 ||
      character.charCodeAt(0) === 127,
  );
}

export const LoginCallbackSchema = z.string().transform((input, context) => {
  const reject = () => {
    context.addIssue({
      code: "custom",
      message: "安全なサイト内の復帰先を指定してください。",
    });
    return z.NEVER;
  };
  if (
    !input.startsWith("/") ||
    input.startsWith("//") ||
    input.trim() !== input ||
    hasUnsafeUrlCharacters(input)
  )
    return reject();

  try {
    // デコード値は検査だけに使い、クエリ内の区切り文字をURL構造へ昇格させない。
    if (hasUnsafeUrlCharacters(decodeURIComponent(input))) return reject();
    const rawPath = input.split(/[?#]/, 1)[0] ?? "";
    if (ENCODED_PATH_SEPARATOR.test(rawPath)) return reject();
    if (PERCENT_ENCODED_BYTE.test(decodeURIComponent(rawPath))) return reject();

    const url = new URL(input, VALIDATION_ORIGIN);
    if (url.origin !== VALIDATION_ORIGIN || url.pathname.startsWith("//"))
      return reject();
    const decodedPath = decodeURIComponent(url.pathname);
    if (decodedPath === "/auth" || decodedPath.startsWith("/auth/"))
      return reject();
    if (url.searchParams.has("_rsc")) url.searchParams.delete("_rsc");
    return url.pathname + url.search + url.hash;
  } catch {
    return reject();
  }
});

export function parseLoginCallback(input: unknown): string | null {
  const result = LoginCallbackSchema.safeParse(input);
  return result.success ? result.data : null;
}
