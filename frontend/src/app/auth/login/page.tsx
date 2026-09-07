import { LoginForm } from "@/features/auth";
import { parseLoginCallback } from "@/lib/auth/login-callback";
import type { SearchParams } from "@/lib/types/route";

export const metadata = {
  title: "ログイン - Vector",
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const { callbackUrl } = await searchParams;
  const returnTo = parseLoginCallback(callbackUrl) ?? "/";
  return (
    <main className="flex min-h-dvh items-center justify-center p-4">
      <LoginForm returnTo={returnTo} />
    </main>
  );
}
