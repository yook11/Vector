"use client";

import { MobileNav } from "@/components/layout/MobileNav";
import { useProtectedNavItems } from "@/components/layout/useProtectedNavItems";

/**
 * シェル用の mobile メニュー。nav 項目を client session から導出し、汎用
 * MobileNav に渡す薄い client wrapper (MobileNav 自体は items 注入式のまま、
 * DashboardMasthead / admin Header と共有するため API を変えない)。
 */
export function ShellMobileNav() {
  const items = useProtectedNavItems();
  return (
    <MobileNav items={items} triggerClassName="sm:inline-flex md:hidden" />
  );
}
