import { Loader2Icon } from "lucide-react";

/** nonce と認証境界が解決するまで、リクエスト固有の情報を含めずに表示する。 */
export function AppBootstrapLoading() {
  return (
    <main className="flex min-h-dvh items-center justify-center bg-stone-50 px-5 text-stone-950 dark:bg-stone-950 dark:text-stone-50">
      <section
        aria-live="polite"
        aria-atomic="true"
        className="w-full max-w-sm rounded-2xl border border-stone-300 bg-white px-6 py-7 shadow-sm dark:border-stone-700 dark:bg-stone-900"
      >
        <p className="text-xs font-semibold tracking-[0.2em] text-teal-700 dark:text-teal-300">
          VECTOR
        </p>
        <div className="mt-4 flex items-center gap-3" role="status">
          <Loader2Icon
            aria-hidden="true"
            className="size-4 shrink-0 animate-spin motion-reduce:animate-none"
          />
          <p className="text-sm font-medium">画面を準備しています…</p>
        </div>
      </section>
    </main>
  );
}
