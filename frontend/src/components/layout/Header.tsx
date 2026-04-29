import Link from "next/link";
import { MobileNav } from "@/components/layout/MobileNav";
import { NavLink } from "@/components/layout/NavLink";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import { UserMenu } from "@/features/auth";

const navItems = [
  { href: "/", label: "ニュース" },
  { href: "/weekly-trends", label: "ウィークリー" },
  { href: "/watchlist", label: "ウォッチリスト" },
  { href: "/settings", label: "マイページ" },
];

export function Header() {
  return (
    <header className="fixed top-0 z-50 w-full border-0 bg-background/70 backdrop-blur-xl">
      <div className="mx-auto grid h-11 grid-cols-[1fr_auto_1fr] items-center px-5 sm:px-8">
        <div className="flex items-center">
          <Link
            href="/"
            className="text-sm font-semibold tracking-tight opacity-90 transition-opacity hover:opacity-100"
          >
            Vector
          </Link>
        </div>

        <nav className="hidden sm:flex items-center gap-7">
          {navItems.map((item) => (
            <NavLink
              key={item.href}
              href={item.href}
              className="text-xs text-foreground/60 transition-colors duration-300 hover:text-foreground"
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="flex items-center justify-end gap-1">
          <ThemeToggle />
          <UserMenu />
          <MobileNav items={navItems} />
        </div>
      </div>
    </header>
  );
}
