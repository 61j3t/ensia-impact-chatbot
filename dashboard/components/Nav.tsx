"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const links = [
  { href: "/", label: "Users" },
  { href: "/data", label: "Data" },
];

export function Nav() {
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);

  // Auto-close the mobile menu on navigation.
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  // Hide the nav entirely on /login — the login page is its own world.
  if (pathname === "/login") return null;

  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  async function signOut() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.replace("/login");
    router.refresh();
  }

  return (
    <header className="sticky top-0 z-40 border-b-2 border-ink-900 bg-cream-50/85 backdrop-blur">
      <div className="relative max-w-7xl mx-auto px-4 sm:px-6 py-3 flex items-center gap-6">
        <Link href="/" className="flex items-center gap-2 group shrink-0">
          <span className="inline-flex h-8 w-8 items-center justify-center rounded-2xl bg-coral-500 text-cream-50 font-black shadow-brutal ring-2 ring-ink-900 group-hover:rotate-[-6deg] transition-transform">
            E
          </span>
          <span className="font-display font-bold text-base sm:text-lg tracking-tight">
            ENSIA Bot
            <span className="hidden sm:inline font-sans font-medium text-ink-900/55 ml-1">
              dashboard
            </span>
          </span>
        </Link>

        {/* ── desktop: inline pills ── */}
        <nav className="hidden md:flex ml-auto items-center gap-1">
          {links.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className={
                "nav-pill " +
                (isActive(l.href) ? "nav-pill-active" : "nav-pill-idle")
              }
            >
              {l.label}
            </Link>
          ))}
          <button
            type="button"
            onClick={signOut}
            className="nav-pill nav-pill-idle ml-1"
            title="Sign out"
          >
            ↪ Sign out
          </button>
        </nav>

        {/* ── mobile: hamburger ── */}
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-label={open ? "Close menu" : "Open menu"}
          aria-expanded={open}
          aria-controls="mobile-nav"
          className="md:hidden ml-auto inline-flex items-center justify-center h-9 w-9 rounded-2xl border-2 border-ink-900 bg-white shadow-brutal hover:bg-cream-100 transition-colors"
        >
          <HamburgerIcon open={open} />
        </button>
      </div>

      {/* ── mobile drop panel — absolutely positioned so it overlays the
            page below instead of pushing it down. The header keeps its
            own height, the panel just appears underneath. ── */}
      <div
        id="mobile-nav"
        className={
          "md:hidden absolute left-0 right-0 top-full origin-top bg-cream-50 border-b-2 border-ink-900 shadow-brutal transition-[transform,opacity] duration-200 " +
          (open
            ? "scale-y-100 opacity-100 pointer-events-auto"
            : "scale-y-0 opacity-0 pointer-events-none")
        }
      >
        <nav className="max-w-7xl mx-auto px-4 py-3 flex flex-col gap-1">
          {links.map((l) => {
            const active = isActive(l.href);
            return (
              <Link
                key={l.href}
                href={l.href}
                className={
                  "rounded-2xl px-4 py-3 text-base font-semibold transition-colors border-2 " +
                  (active
                    ? "border-ink-900 bg-ink-900 text-cream-50 shadow-brutal"
                    : "border-transparent text-ink-900 hover:bg-cream-100")
                }
              >
                {l.label}
              </Link>
            );
          })}
          <button
            type="button"
            onClick={signOut}
            className="text-left rounded-2xl px-4 py-3 text-base font-semibold transition-colors border-2 border-transparent text-ink-900 hover:bg-cream-100"
          >
            ↪ Sign out
          </button>
        </nav>
      </div>

      {/* Click-anywhere-else-to-close backdrop. Pointer-only — doesn't
          steal focus or block scrolling on the body underneath. */}
      {open && (
        <div
          aria-hidden
          onClick={() => setOpen(false)}
          className="md:hidden fixed inset-0 top-[57px] z-30 bg-transparent"
        />
      )}
    </header>
  );
}

function HamburgerIcon({ open }: { open: boolean }) {
  // Two bars that morph into an × when open. Pure CSS, no SVG.
  return (
    <span className="relative inline-block h-4 w-5">
      <span
        className={
          "absolute left-0 right-0 h-0.5 bg-ink-900 rounded-full transition-transform duration-200 " +
          (open ? "top-1/2 -translate-y-1/2 rotate-45" : "top-1")
        }
      />
      <span
        className={
          "absolute left-0 right-0 h-0.5 bg-ink-900 rounded-full transition-transform duration-200 " +
          (open ? "top-1/2 -translate-y-1/2 -rotate-45" : "bottom-1")
        }
      />
    </span>
  );
}
