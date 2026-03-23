"use client";

import { Menu } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import type { CategoryDetailResponse } from "@/types";
import { CategorySidebar } from "./CategorySidebar";

interface MobileSidebarProps {
  categories: CategoryDetailResponse[];
  activeKwCategoryId?: number;
  activeKeywordId?: number;
  subscribedKeywordIds?: number[];
  showMyKeywords?: boolean;
}

export function MobileSidebar({
  categories,
  activeKwCategoryId,
  activeKeywordId,
  subscribedKeywordIds,
  showMyKeywords,
}: MobileSidebarProps) {
  const [open, setOpen] = useState(false);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button variant="ghost" size="icon" className="lg:hidden">
          <Menu className="h-5 w-5" />
          <span className="sr-only">Toggle sidebar</span>
        </Button>
      </SheetTrigger>
      <SheetContent side="left" className="w-64 p-0">
        <SheetHeader className="p-4 pb-0">
          <SheetTitle>Filters</SheetTitle>
        </SheetHeader>
        {/* biome-ignore lint/a11y/noStaticElementInteractions: event delegation to close sheet on sidebar link clicks */}
        <div role="presentation" onClick={() => setOpen(false)}>
          <CategorySidebar
            categories={categories}
            activeKwCategoryId={activeKwCategoryId}
            activeKeywordId={activeKeywordId}
            subscribedKeywordIds={subscribedKeywordIds}
            showMyKeywords={showMyKeywords}
          />
        </div>
      </SheetContent>
    </Sheet>
  );
}
