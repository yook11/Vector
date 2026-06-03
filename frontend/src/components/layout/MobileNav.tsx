"use client";

import { Menu } from "lucide-react";
import { useState } from "react";
import { NavLink } from "@/components/layout/NavLink";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { cn } from "@/lib/utils/cn";

interface MobileNavProps {
  items: Array<{ href: string; label: string }>;
  triggerClassName?: string;
}

export function MobileNav({ items, triggerClassName }: MobileNavProps) {
  const [open, setOpen] = useState(false);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className={cn("h-8 w-8 sm:hidden", triggerClassName)}
        >
          <Menu aria-hidden="true" className="size-4" />
          <span className="sr-only">メニュー</span>
        </Button>
      </SheetTrigger>
      <SheetContent
        side="right"
        className="w-72 border-l-0 bg-background/95 backdrop-blur-2xl"
      >
        <SheetHeader>
          <SheetTitle className="text-left text-sm font-medium tracking-tight">
            Vector
          </SheetTitle>
          <SheetDescription className="sr-only">
            主要ページへのナビゲーション
          </SheetDescription>
        </SheetHeader>
        <nav className="flex flex-col gap-1 mt-10">
          {items.map((item) => (
            <NavLink
              key={item.href}
              href={item.href}
              onClick={() => setOpen(false)}
              className="px-3 py-2.5 text-sm text-muted-foreground rounded-xl transition-colors duration-200 hover:text-foreground hover:bg-accent"
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </SheetContent>
    </Sheet>
  );
}
