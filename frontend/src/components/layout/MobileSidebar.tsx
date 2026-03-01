"use client";

import { useState } from "react";
import { Menu } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { CategorySidebar } from "./CategorySidebar";
import type { KeywordCategoryDetailResponse } from "@/types";

interface MobileSidebarProps {
  categories: KeywordCategoryDetailResponse[];
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
        <div onClick={() => setOpen(false)}>
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
