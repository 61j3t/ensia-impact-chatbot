import type { Metadata } from "next";
import { Inter_Tight, Fraunces } from "next/font/google";
import "./globals.css";
import { Nav } from "@/components/Nav";

// Geist isn't on Google Fonts; Inter Tight gives a similar modern, slightly
// condensed bold sans. Fraunces is the serif used for the display numerals
// — variable weight + italic gives the editorial flourish.
const sans = Inter_Tight({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
  variable: "--font-sans",
  display: "swap",
});
const display = Fraunces({
  subsets: ["latin"],
  weight: ["500", "600", "700", "800"],
  style: ["normal", "italic"],
  variable: "--font-display",
  display: "swap",
});

export const metadata: Metadata = {
  title: "ENSIA Bot Dashboard",
  description:
    "Live activity & user data for the ENSIA Impact Telegram bot.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    // `suppressHydrationWarning` keeps browser-extension injected attrs
    // (Scribe, Grammarly) from blocking hydration. It only suppresses
    // one level — real app-rendered mismatches still throw.
    <html
      lang="en"
      suppressHydrationWarning
      className={`${sans.variable} ${display.variable}`}
    >
      <body
        className="min-h-screen font-sans text-ink-900"
        suppressHydrationWarning
      >
        <Nav />
        <main className="max-w-7xl mx-auto px-4 sm:px-6 py-6 sm:py-10">
          {children}
        </main>
      </body>
    </html>
  );
}
