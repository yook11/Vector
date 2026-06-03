export type ProtectedNavItem = {
  href: string;
  label: string;
};

const baseNavItems = [
  { href: "/", label: "ニュース" },
  { href: "/briefing", label: "Briefing" },
  { href: "/weekly-trends", label: "ウィークリー" },
  { href: "/watchlist", label: "ウォッチリスト" },
] satisfies ProtectedNavItem[];

const adminNavItem = {
  href: "/settings",
  label: "Settings",
} satisfies ProtectedNavItem;

export function getProtectedNavItems(isAdmin: boolean): ProtectedNavItem[] {
  return isAdmin ? [...baseNavItems, adminNavItem] : baseNavItems;
}
