/**
 * Next.js の `redirect()` が投げる特殊 error を判定する。
 *
 * Next.js 16.2 時点で `isRedirectError` は公式 export されていないため、
 * digest プロパティで判定する (Next.js 13 から digest =
 * "NEXT_REDIRECT;<type>;<url>;<status>" の構造で安定)。
 *
 * Server Action を `try/catch` で呼ぶ Client Component が、未認証時の
 * `requireSessionForAction()` 由来の redirect throw を握り潰してしまうと、
 * Next.js の navigation 機構が起動せず login 画面へ遷移しない silent fail
 * になる。各 catch 先頭でこの helper を呼び、true なら必ず re-throw する。
 *
 * Next.js が将来公式 export を提供した際の差し替えも本ファイル 1 箇所で完結する。
 */
export function isRedirectError(err: unknown): boolean {
  return (
    err !== null &&
    typeof err === "object" &&
    "digest" in err &&
    typeof (err as { digest: unknown }).digest === "string" &&
    (err as { digest: string }).digest.startsWith("NEXT_REDIRECT")
  );
}
