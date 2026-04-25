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
  activeCategory?: string;
}

export function MobileSidebar({
  categories,
  activeCategory,
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
      <SheetContent
        side="left"
        className="w-72 p-0 bg-background/95 backdrop-blur-2xl border-r-0"
      >
        <SheetHeader className="p-5 pb-0">
          <SheetTitle className="text-sm font-medium">Filters</SheetTitle>
        </SheetHeader>
        {/* biome-ignore lint/a11y/noStaticElementInteractions: event delegation to close sheet on sidebar link clicks */}
        <div role="presentation" onClick={() => setOpen(false)}>
          <CategorySidebar
            categories={categories}
            activeCategory={activeCategory}
          />
        </div>
      </SheetContent>
    </Sheet>
  );
}
