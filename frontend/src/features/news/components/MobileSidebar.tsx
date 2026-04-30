"use client";

import { Menu } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import type { CategoryDetailResponse } from "@/types";
import { CategorySidebar } from "./CategorySidebar";

interface MobileSidebarProps {
  categories: CategoryDetailResponse[];
  activeCategory?: string;
}

export function MobileSidebar({
  categories,
  activeCategory,
}: MobileSidebarProps) {
  const [open, setOpen] = useState(false);

  // EOP 下で undefined を CategorySidebar の optional activeCategory に
  // 明示代入できないため、条件付き spread で「未指定 or 値あり」を表現する。
  const categoryProps = activeCategory !== undefined ? { activeCategory } : {};

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button variant="ghost" size="icon" className="lg:hidden">
          <Menu aria-hidden="true" className="h-5 w-5" />
          <span className="sr-only">サイドバーを開く</span>
        </Button>
      </SheetTrigger>
      <SheetContent
        side="left"
        className="w-72 p-0 bg-background/95 backdrop-blur-2xl border-r-0"
      >
        <SheetHeader className="p-5 pb-0">
          <SheetTitle className="text-sm font-medium">Filters</SheetTitle>
          <SheetDescription className="sr-only">
            カテゴリでニュース一覧を絞り込む
          </SheetDescription>
        </SheetHeader>
        <CategorySidebar
          categories={categories}
          {...categoryProps}
          onNavigate={() => setOpen(false)}
        />
      </SheetContent>
    </Sheet>
  );
}
