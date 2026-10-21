"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

const nav = [
  { href: "/dashboard", label: "Visão geral" },
  { href: "/dashboard/accounts", label: "Contas" },
  { href: "/dashboard/bots", label: "Bots" },
  { href: "/dashboard/strategies", label: "Estratégias & Backtest" },
  { href: "/dashboard/scanner", label: "Scanner" },
  { href: "/dashboard/news", label: "Notícias" },
  { href: "/dashboard/calendar", label: "Calendário" },
  { href: "/dashboard/history", label: "Histórico" },
  { href: "/dashboard/monitoring", label: "Monitoramento" },
  { href: "/dashboard/workspaces", label: "Workspaces" },
  { href: "/dashboard/profile", label: "Perfil" },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { user, workspaces, workspace, loading, switchWorkspace, logout } = useAuth();

  if (loading || !user) {
    return (
      <div className="flex min-h-screen">
        <aside className="hidden w-60 border-r p-4 md:block">
          <Skeleton className="h-6 w-32" />
        </aside>
        <main className="flex-1 space-y-4 p-6">
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-32 w-full" />
        </main>
      </div>
    );
  }

  const items = user.role === "admin" ? [...nav, { href: "/admin", label: "Administração" }] : nav;

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <aside className="border-b p-4 md:w-60 md:border-b-0 md:border-r">
        <Link href="/dashboard" className="mb-4 block font-semibold">
          Trading Platform
        </Link>
        <label className="mb-1 block text-xs text-muted-foreground" htmlFor="ws-switch">
          Workspace
        </label>
        <Select
          id="ws-switch"
          value={workspace?.id ?? ""}
          onChange={(e) => switchWorkspace(e.target.value).then(() => window.location.reload())}
          className="mb-4 h-9"
        >
          {workspaces.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </Select>
        <nav className="flex flex-wrap gap-1 text-sm md:flex-col">
          {items.map((n) => (
            <Link
              key={n.href}
              href={n.href}
              className={cn(
                "rounded px-2 py-1.5 hover:bg-accent",
                (pathname === n.href || (n.href !== "/dashboard" && pathname.startsWith(n.href))) && "bg-accent"
              )}
            >
              {n.label}
            </Link>
          ))}
        </nav>
        <div className="mt-6 border-t pt-4 text-xs text-muted-foreground">
          <p className="truncate">{user.email}</p>
          <p>Papel no workspace: {workspace?.role ?? "—"}</p>
          <Button variant="ghost" size="sm" className="mt-2 px-2" onClick={logout}>
            Sair
          </Button>
        </div>
      </aside>
      <main className="flex-1 overflow-x-hidden p-4 md:p-6">{children}</main>
    </div>
  );
}
