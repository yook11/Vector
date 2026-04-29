import { toast } from "sonner";

/**
 * React 19 / Next.js 16 は production build で Server Action throw の
 * `error.message` を以下文言にマスクする (backend 内部情報の漏洩防止):
 *
 *   "An error occurred in the Server Components render. The specific message
 *    is omitted in production builds to avoid leaking sensitive details. ..."
 *
 * これがそのまま toast に出ると UX を損なうので、先頭一致で検出して
 * caller の fallback 文言に置換する。
 */
const REACT_PROD_MASK_PREFIX = "An error occurred in the Server";

/**
 * Server Action の throw を toast.error に整形して表示する。
 *
 * - dev / 自前 throw: `err.message` をそのまま表示
 * - production マスク文言: `fallback` に置換
 * - Error 以外: `fallback` に置換
 */
export function toastError(err: unknown, fallback: string): void {
  if (!(err instanceof Error) || !err.message) {
    toast.error(fallback);
    return;
  }
  if (err.message.startsWith(REACT_PROD_MASK_PREFIX)) {
    toast.error(fallback);
    return;
  }
  toast.error(err.message);
}
