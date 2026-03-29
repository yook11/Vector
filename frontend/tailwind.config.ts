import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      /* --- Typography: Plus Jakarta Sans via CSS variable --- */
      fontFamily: {
        sans: [
          "var(--font-sans)",
          "system-ui",
          "-apple-system",
          "BlinkMacSystemFont",
          "sans-serif",
        ],
      },
      /* Apple-like type scale: tight tracking, generous leading */
      fontSize: {
        xs: ["0.75rem", { lineHeight: "1.125rem", letterSpacing: "-0.006em" }],
        sm: ["0.875rem", { lineHeight: "1.375rem", letterSpacing: "-0.008em" }],
        base: ["1rem", { lineHeight: "1.625rem", letterSpacing: "-0.011em" }],
        lg: ["1.125rem", { lineHeight: "1.75rem", letterSpacing: "-0.014em" }],
        xl: ["1.25rem", { lineHeight: "1.875rem", letterSpacing: "-0.017em" }],
        "2xl": ["1.5rem", { lineHeight: "2rem", letterSpacing: "-0.019em" }],
        "3xl": [
          "1.875rem",
          { lineHeight: "2.375rem", letterSpacing: "-0.022em" },
        ],
        "4xl": [
          "2.25rem",
          { lineHeight: "2.75rem", letterSpacing: "-0.025em" },
        ],
        "5xl": ["3rem", { lineHeight: "3.5rem", letterSpacing: "-0.028em" }],
        "6xl": [
          "3.75rem",
          { lineHeight: "4.25rem", letterSpacing: "-0.032em" },
        ],
      },
      letterSpacing: {
        tightest: "-0.04em",
        tighter: "-0.025em",
        tight: "-0.015em",
        normal: "-0.011em",
      },

      /* --- Spacing: extended scale for generous Apple-like whitespace --- */
      spacing: {
        "18": "4.5rem",
        "22": "5.5rem",
        "26": "6.5rem",
        "30": "7.5rem",
        "34": "8.5rem",
        "38": "9.5rem",
      },

      /* --- Border radius: generous rounding --- */
      borderRadius: {
        "2xl": "calc(var(--radius) + 8px)",
        xl: "calc(var(--radius) + 4px)",
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },

      /* --- Shadows: subtle, diffused Apple-style elevation --- */
      boxShadow: {
        subtle: "0 1px 2px rgba(0, 0, 0, 0.04)",
        soft: "0 2px 10px rgba(0, 0, 0, 0.06)",
        elevated: "0 8px 30px rgba(0, 0, 0, 0.08)",
        float: "0 20px 60px rgba(0, 0, 0, 0.12)",
      },

      /* --- Motion: Apple-like easing --- */
      transitionTimingFunction: {
        apple: "cubic-bezier(0.25, 0.1, 0.25, 1)",
        "apple-bounce": "cubic-bezier(0.34, 1.56, 0.64, 1)",
      },
      transitionDuration: {
        DEFAULT: "250ms",
        fast: "150ms",
        slow: "400ms",
      },

      /* --- Colors: shadcn/ui CSS variable integration --- */
      colors: {
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        chart: {
          "1": "hsl(var(--chart-1))",
          "2": "hsl(var(--chart-2))",
          "3": "hsl(var(--chart-3))",
          "4": "hsl(var(--chart-4))",
          "5": "hsl(var(--chart-5))",
        },
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

export default config;
