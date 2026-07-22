import Link from "next/link";

export function ProvisionUserLink() {
  return (
    <Link
      href="/admin/users/new"
      className="inline-block text-xs text-muted-foreground underline underline-offset-4 hover:text-foreground"
    >
      デモユーザーを登録
    </Link>
  );
}
