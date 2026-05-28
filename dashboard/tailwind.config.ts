import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        // Wired in app/layout.tsx via next/font/google.
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        display: ["var(--font-display)", "Georgia", "serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      colors: {
        cream: {
          50: "#FFFCF5",
          100: "#FFF8EC",
          200: "#FBEFD9",
        },
        ink: {
          900: "#0E1116",
          800: "#1B1F26",
          700: "#2C3038",
        },
        coral: {
          50: "#FFF1F6",
          100: "#FFD7E6",
          400: "#FF7AB6",
          500: "#FF4D94",
          600: "#E33A7E",
        },
        lemon: {
          400: "#FFD93D",
          500: "#F4C20D",
        },
        ocean: {
          50: "#EEF6FF",
          400: "#5BA3FF",
          500: "#2F7BFF",
          600: "#1F5FE0",
        },
        moss: {
          50: "#ECFDF3",
          400: "#34D399",
          500: "#10B981",
        },
        ember: {
          50: "#FFF1ED",
          400: "#FF8A65",
          500: "#FF5722",
        },
      },
      borderRadius: {
        "2.5xl": "1.25rem",
        "3xl": "1.5rem",
      },
      boxShadow: {
        // 2D "brutalist" drop — solid offset under each card.
        brutal: "4px 4px 0 0 #0E1116",
        brutalHover: "6px 6px 0 0 #0E1116",
        soft: "0 8px 24px -12px rgba(15,17,22,0.18)",
      },
      letterSpacing: {
        kicker: "0.16em",
      },
    },
  },
  plugins: [],
};

export default config;
