import Link from "next/link";

const links = [
  { href: "/", label: "Overview" },
  { href: "/users", label: "Users" },
  { href: "/conversations", label: "Conversations" },
];

export function Nav() {
  return (
    <header className="border-b border-zinc-200 bg-white">
      <div className="max-w-6xl mx-auto px-6 py-4 flex items-center gap-8">
        <Link href="/" className="font-semibold tracking-tight text-zinc-900">
          ENSIA Bot · dashboard
        </Link>
        <nav className="flex gap-5 text-sm text-zinc-600">
          {links.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className="hover:text-zinc-900 transition-colors"
            >
              {l.label}
            </Link>
          ))}
        </nav>
      </div>
    </header>
  );
}
