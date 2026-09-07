import { LoginForm } from "@/features/auth";
import { parseLoginCallback } from "@/lib/auth/login-callback";
import { isPublicPage } from "@/lib/auth/page-access";
import type { SearchParams } from "@/lib/types/route";

export const metadata = {
  title: "ログイン - Vector",
};

function callbackPathname(path: string): string {
  return path.split(/[?#]/, 1)[0] ?? "";
}

function loginFormAccess(callbackUrl: unknown) {
  const parsed = parseLoginCallback(callbackUrl);
  const returnTo = parsed ?? "/";
  const backHref = isPublicPage(callbackPathname(returnTo)) ? returnTo : "/";
  return {
    returnTo,
    requiresLoginReason: parsed !== null,
    backHref,
    backLabel:
      callbackPathname(backHref) === "/"
        ? "ニュースへ戻る"
        : "元のページへ戻る",
  };
}

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const { callbackUrl } = await searchParams;
  return (
    <main className="flex min-h-dvh items-center justify-center p-4">
      <LoginForm {...loginFormAccess(callbackUrl)} />
    </main>
  );
}
