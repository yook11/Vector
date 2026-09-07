import { PageNavigationProvider } from "@/components/layout/PageNavigation";

export default function PublicLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <PageNavigationProvider>{children}</PageNavigationProvider>;
}
