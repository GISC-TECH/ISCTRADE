import Link from "next/link";

const nav = [
  { href: "/dashboard", label: "Visão geral" },
  { href: "/dashboard/accounts", label: "Contas" },
  { href: "/dashboard/workspaces", label: "Workspaces" },
  { href: "/dashboard/profile", label: "Perfil" },
];

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-56 border-r p-4 md:block">
        <p className="mb-6 font-semibold">Trading Platform</p>
        <nav className="flex flex-col gap-2 text-sm">
          {nav.map((n) => (
            <Link key={n.href} href={n.href} className="rounded px-2 py-1 hover:bg-accent">
              {n.label}
            </Link>
          ))}
        </nav>
      </aside>
      <main className="flex-1 p-6">{children}</main>
    </div>
  );
}
