import { requireSession } from "@/lib/auth/guards";

export default async function ProtectedLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  await requireSession();
  return children;
}
