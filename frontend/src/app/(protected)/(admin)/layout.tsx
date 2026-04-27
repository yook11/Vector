import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { auth } from "@/lib/auth";

// admin role を持つユーザのみ通す route segment。
// (protected) layout で session 自体は担保済みなので、ここでは role 判定だけ行う。
export default async function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const session = await auth.api.getSession({
    headers: await headers(),
  });

  if (session?.user.role !== "admin") {
    redirect("/");
  }

  return <>{children}</>;
}
