import { redirect } from "next/navigation";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";

export default async function ProtectedLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const session = await getServerSession(authOptions);

  // Not logged in (defensive: middleware normally catches this first)
  if (!session) {
    redirect("/auth/login");
  }
  // Refresh token failed — session cookie exists but tokens are invalid
  if (session.error === "RefreshTokenError") {
    redirect("/auth/login?error=SessionExpired");
  }
  // Access token is empty (set to "" when undefined after refresh failure)
  if (!session.accessToken) {
    redirect("/auth/login");
  }

  return <>{children}</>;
}
