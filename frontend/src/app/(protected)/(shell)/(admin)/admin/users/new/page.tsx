import type { Metadata } from "next";
import { PageContainer } from "@/components/layout/PageContainer";
import { ProvisionUserForm } from "@/features/auth";
import { requireAdmin } from "@/lib/auth/guards";

export const metadata: Metadata = {
  title: "デモユーザーを登録 | Vector",
};

export default async function ProvisionUserPage() {
  await requireAdmin();

  return (
    <PageContainer maxWidth="4xl">
      <div className="max-w-xl">
        <h1 className="text-base font-medium">デモユーザーを登録</h1>
        <p className="mt-2 text-xs text-muted-foreground">
          一般ユーザーのログイン情報を発行します。
        </p>
      </div>
      <ProvisionUserForm />
    </PageContainer>
  );
}
