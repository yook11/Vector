import { describe, expect, it } from "vitest";
import { isRedirectError } from "./redirect-error";

// Next.js の redirect() は実体として
//   throw Object.assign(new Error("NEXT_REDIRECT"), {
//     digest: "NEXT_REDIRECT;<type>;<url>;<status>;",
//   })
// に等価な構造の error を投げる (Next.js 13+ で安定)。test では本物の
// next/navigation.redirect() を呼ばず、digest 文字列を直接組み立てて
// 判定ロジックを純粋に検証する (Next.js minor up で文言が変わっても
// 本 test 1 ファイルの修正で追従できる安全弁)。
function makeRedirectError(
  type: "replace" | "push" = "replace",
  url = "/auth/login",
  status = 307,
): Error & { digest: string } {
  return Object.assign(new Error("NEXT_REDIRECT"), {
    digest: `NEXT_REDIRECT;${type};${url};${status};`,
  });
}

describe("isRedirectError — true 判定", () => {
  it("Next.js redirect 形状 (replace, /auth/login) は true", () => {
    expect(isRedirectError(makeRedirectError("replace", "/auth/login"))).toBe(
      true,
    );
  });

  it("push 型 redirect も true (digest prefix だけで判定)", () => {
    expect(isRedirectError(makeRedirectError("push", "/foo", 303))).toBe(true);
  });

  it("digest = 'NEXT_REDIRECT' のみ (suffix なし) でも true", () => {
    expect(isRedirectError({ digest: "NEXT_REDIRECT" })).toBe(true);
  });
});

describe("isRedirectError — false 判定", () => {
  it("一般 Error は false (digest なし)", () => {
    expect(isRedirectError(new Error("Network down"))).toBe(false);
  });

  it("notFound() 由来 (digest = NEXT_NOT_FOUND) は false — 本 helper の対象外", () => {
    // notFound() throw は Server Component の rendering 経路で扱う。Server
    // Action catch では現れない想定だが、誤判定しないことを構造的に保証する。
    expect(isRedirectError({ digest: "NEXT_NOT_FOUND" })).toBe(false);
  });

  it("digest が string でない (number) は false", () => {
    expect(isRedirectError({ digest: 307 })).toBe(false);
  });

  it("digest が prefix 不一致 (HTTP_REDIRECT) は false", () => {
    expect(isRedirectError({ digest: "HTTP_REDIRECT;replace;/" })).toBe(false);
  });

  it("null は false", () => {
    expect(isRedirectError(null)).toBe(false);
  });

  it("undefined は false", () => {
    expect(isRedirectError(undefined)).toBe(false);
  });

  it("string は false", () => {
    expect(isRedirectError("NEXT_REDIRECT;replace;/")).toBe(false);
  });

  it("plain object (digest プロパティなし) は false", () => {
    expect(isRedirectError({ message: "looks like error" })).toBe(false);
  });
});
