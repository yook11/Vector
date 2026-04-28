import { headers } from "next/headers";
import { ThemeProvider } from "next-themes";
import type { ReactNode } from "react";

/**
 * proxy で生成された CSP nonce をリクエストヘッダーから読み取り、ThemeProvider に渡す。
 *
 * next-themes はテーマ検出のためにインラインスクリプトを注入するが、nonce を付与する
 * ことで CSP の script-src ポリシーに準拠させる。
 *
 * `headers()` は dynamic API のため、`cacheComponents: true` 下では Suspense 境界の
 * 内側で呼ぶ必要がある。本コンポーネントを `<Suspense>` で包むことで、隔離された
 * dynamic boundary として機能させる。
 */
export async function NonceThemeProvider({
  children,
}: {
  children: ReactNode;
}) {
  const nonce = (await headers()).get("x-nonce") ?? "";
  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      nonce={nonce}
    >
      {children}
    </ThemeProvider>
  );
}
