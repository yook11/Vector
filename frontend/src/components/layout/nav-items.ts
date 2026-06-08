import {
  Eye,
  FileText,
  type LucideIcon,
  Newspaper,
  Settings,
  TrendingUp,
} from "lucide-react";

// icon はキー文字列で持つ。LucideIcon (関数) を nav item に焼くと Server→Client
// 境界 (MobileNav へ navItems を渡す箇所) でシリアライズ不能になるため、
// コンポーネント解決は各 consumer の render 時に NAV_ICONS 経由で行う。
export type NavIconKey =
  | "news"
  | "briefing"
  | "trend"
  | "watchlist"
  | "settings";

export const NAV_ICONS: Record<NavIconKey, LucideIcon> = {
  news: Newspaper,
  briefing: FileText,
  trend: TrendingUp,
  watchlist: Eye,
  settings: Settings,
};

export type ProtectedNavItem = {
  href: string;
  label: string;
  icon: NavIconKey;
};

const baseNavItems = [
  { href: "/", label: "ニュース", icon: "news" },
  { href: "/briefing", label: "Briefing", icon: "briefing" },
  { href: "/trends", label: "トレンド", icon: "trend" },
  { href: "/watchlist", label: "ウォッチリスト", icon: "watchlist" },
] satisfies ProtectedNavItem[];

const adminNavItem = {
  href: "/settings",
  label: "Settings",
  icon: "settings",
} satisfies ProtectedNavItem;

export function getProtectedNavItems(isAdmin: boolean): ProtectedNavItem[] {
  return isAdmin ? [...baseNavItems, adminNavItem] : baseNavItems;
}
