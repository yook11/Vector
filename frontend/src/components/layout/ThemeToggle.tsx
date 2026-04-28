"use client";

import { Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";

export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  // mounted ガード: SSR 時は theme を解決できず、クライアント初期描画でも
  // localStorage を読むまで `resolvedTheme` が `undefined`。マウント前に
  // アイコンを描画すると hydration mismatch / 初描画でアイコン点滅が起きる。
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) {
    return (
      <Button
        variant="ghost"
        size="icon"
        disabled
        aria-hidden="true"
        tabIndex={-1}
      />
    );
  }

  const isDark = resolvedTheme === "dark";
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      aria-label={isDark ? "ライトテーマに切り替え" : "ダークテーマに切り替え"}
    >
      <Sun
        aria-hidden="true"
        className="h-5 w-5 rotate-0 scale-100 transition-all dark:-rotate-90 dark:scale-0"
      />
      <Moon
        aria-hidden="true"
        className="absolute h-5 w-5 rotate-90 scale-0 transition-all dark:rotate-0 dark:scale-100"
      />
    </Button>
  );
}
