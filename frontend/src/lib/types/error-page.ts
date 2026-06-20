/**
 * Next.js App Router の error.tsx / global-error.tsx 用 props 共通型。
 *
 * Next.js 公式仕様: `error` + `reset` + `unstable_retry` (v16.2.0 で追加)。
 * Server Component で発生した error は `digest` (server-side hash) を持ち、
 * client への詳細漏洩を防ぐ。production build では `error.message` も自動
 * マスクされるため、UI 側は generic 文言を表示する責務に集中する。
 *
 * `reset()` は error state を消して再 render するだけで再 fetch しない。
 * `unstable_retry()` は再 fetch して復帰を試みるため、復帰用途ではこちらを使う。
 */
export interface ErrorPageProps {
  error: Error & { digest?: string };
  reset: () => void;
  unstable_retry: () => void;
}
