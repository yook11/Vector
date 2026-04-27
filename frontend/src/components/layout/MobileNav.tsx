"use client";

import { Menu } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";

interface MobileNavProps {
  items: Array<{ href: string; label: string }>;
}

export function MobileNav({ items }: MobileNavProps) {
  const [open, setOpen] = useState(false);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button variant="ghost" size="icon" className="sm:hidden h-8 w-8">
          <Menu aria-hidden="true" className="h-4 w-4" />
          <span className="sr-only">メニュー</span>
        </Button>
      </SheetTrigger>
      <SheetContent
        side="right"
        className="w-72 border-l-0 bg-neutral-50/95 dark:bg-neutral-950/95 backdrop-blur-2xl"
      >
        <SheetHeader>
          <SheetTitle className="text-left text-sm font-medium tracking-tight">
            Vector
          </SheetTitle>
        </SheetHeader>
        <nav className="flex flex-col gap-1 mt-10">
          {items.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              onClick={() => setOpen(false)}
              className="px-3 py-2.5 text-sm text-muted-foreground rounded-xl transition-colors duration-200 hover:text-foreground hover:bg-neutral-100 dark:hover:bg-neutral-800/40"
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </SheetContent>
    </Sheet>
  );
}
