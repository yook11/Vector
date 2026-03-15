import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { headers } from "next/headers";
import { ThemeProvider } from "next-themes";
import { Toaster } from "sonner";
import { Header } from "@/components/layout/Header";
import { SessionProvider } from "@/components/auth/SessionProvider";
import { AuthErrorWatcher } from "@/components/auth/AuthErrorWatcher";
import "./globals.css";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Vector",
  description: "Tech news aggregation & AI analysis dashboard",
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // --- XSS対策: CSP nonce の受け渡し ---
  //
  // middleware で生成した nonce をリクエストヘッダーから読み取り、
  // next-themes の ThemeProvider に渡す。
  // next-themes はテーマ検出のためにインラインスクリプトを注入するが、
  // nonce を付与することで CSP の script-src ポリシーに準拠させる。
  //
  // Next.js 15 では headers() が非同期API（Promise）に変更されたため await が必要。
  const nonce = (await headers()).get("x-nonce") ?? "";

  return (
    <html lang="ja" suppressHydrationWarning>
      <body className={`${inter.className} bg-background text-foreground`}>
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          nonce={nonce}
        >
          <SessionProvider>
            <AuthErrorWatcher />
            <Header />
            <div className="min-h-[calc(100vh-3.5rem)]">{children}</div>
            <Toaster richColors position="bottom-right" />
          </SessionProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
