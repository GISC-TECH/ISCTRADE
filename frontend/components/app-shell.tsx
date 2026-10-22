"use client";

import { Menu, X } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { DisclaimerGate, LegalFooter } from "@/components/legal";
import { TickerBar } from "@/components/ticker-bar";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import type { User, Workspace } from "@/lib/types";
import { cn } from "@/lib/utils";

const nav = [
  { href: "/dashboard", label: "Visão geral" },
  { href: "/dashboard/live", label: "Tempo real & Trading" },
  { href: "/dashboard/positions", label: "Posições" },
  { href: "/dashboard/pnl", label: "PnL & relatórios" },
  { href: "/dashboard/accounts", label: "Contas" },
  { href: "/dashboard/bots", label: "Bots" },
  { href: "/dashboard/strategies", label: "Estratégias & Backtest" },
  { href: "/dashboard/scanner", label: "Scanner" },
  { href: "/dashboard/news", label: "Notícias" },
  { href: "/dashboard/calendar", label: "Calendário" },
  { href: "/dashboard/history", label: "Histórico" },
  { href: "/dashboard/alerts", label: "Alertas" },
  { href: "/dashboard/monitoring", label: "Monitoramento" },
  { href: "/dashboard/workspaces", label: "Workspaces" },
  { href: "/dashboard/profile", label: "Perfil" },
  { href: "/dashboard/security", label: "Segurança (2FA)" },
];

const ROLE_LABELS: Record<string, string> = { admin: "administrador", trader: "trader", viewer: "leitura" };
const SECURITY_PATH = "/dashboard/security";

function isActive(pathname: string, href: string) {
  return pathname === href || (href !== "/dashboard" && pathname.startsWith(`${href}/`)) || pathname === `${href}/`;
}

function SidebarContent({
  user,
  workspaces,
  workspace,
  pathname,
  onNavigate,
  switchWorkspace,
  logout,
  idSuffix,
}: {
  idSuffix: string;
  user: User;
  workspaces: Workspace[];
  workspace: Workspace | null;
  pathname: string;
  onNavigate: () => void;
  switchWorkspace: (id: string) => Promise<void>;
  logout: () => Promise<void>;
}) {
  const items = user.role === "admin" ? [...nav, { href: "/admin", label: "Administração" }] : nav;
  return (
    <>
      <label className="mb-1 block text-xs text-muted-foreground" htmlFor={`ws-switch-${idSuffix}`}>
        Workspace
      </label>
      <Select
        id={`ws-switch-${idSuffix}`}
        value={workspace?.id ?? ""}
        disabled={!workspaces.length}
        onChange={(e) => switchWorkspace(e.target.value).then(() => window.location.reload())}
        className="mb-4 h-9"
      >
        {workspaces.map((w) => (
          <option key={w.id} value={w.id}>
            {w.name}
          </option>
        ))}
      </Select>
      <nav aria-label="Menu principal" className="flex flex-col gap-0.5 text-sm">
        {items.map((n) => {
          const active = isActive(pathname, n.href);
          return (
            <Link
              key={n.href}
              href={n.href}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              className={cn(
                "rounded px-2 py-1.5 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                active && "bg-accent font-medium"
              )}
            >
              {n.label}
            </Link>
          );
        })}
      </nav>
      <div className="mt-6 border-t pt-4 text-xs text-muted-foreground">
        <p className="truncate" title={user.email}>
          {user.email}
        </p>
        <p>Papel no workspace: {workspace?.role ? ROLE_LABELS[workspace.role] ?? workspace.role : "—"}</p>
        <p>2FA: {user.totp_enabled ? "ativo" : "desativado"}</p>
        <Button variant="ghost" size="sm" className="mt-2 px-2" onClick={logout}>
          Sair
        </Button>
      </div>
    </>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { user, workspaces, workspace, loading, switchWorkspace, logout, reload } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  if (loading || !user) {
    return (
      <div className="flex min-h-screen" aria-busy="true">
        <aside className="hidden w-60 border-r p-4 md:block">
          <Skeleton className="h-6 w-32" />
        </aside>
        <main className="flex-1 space-y-4 p-6">
          <span className="sr-only">Carregando...</span>
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-32 w-full" />
        </main>
      </div>
    );
  }

  const mfaBlocked = !!user.mfa_required && !user.totp_enabled && user.mfa_reason !== "admin";
  const onSecurityPage = pathname.startsWith(SECURITY_PATH);
  const close = () => setMenuOpen(false);
  const sidebarProps = { user, workspaces, workspace, pathname, switchWorkspace, logout };

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <a
        href="#conteudo"
        className="sr-only z-[70] rounded bg-primary px-3 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:left-2 focus:top-2"
      >
        Pular para o conteúdo
      </a>

      {/* Mobile top bar */}
      <header className="sticky top-0 z-40 flex items-center justify-between border-b bg-background/95 px-4 py-2 backdrop-blur md:hidden">
        <Link href="/dashboard" className="font-semibold">
          ISCTRADE
        </Link>
        <Button
          variant="ghost"
          size="sm"
          aria-label={menuOpen ? "Fechar menu" : "Abrir menu"}
          aria-expanded={menuOpen}
          aria-controls="menu-movel"
          onClick={() => setMenuOpen((o) => !o)}
        >
          {menuOpen ? <X className="h-5 w-5" aria-hidden /> : <Menu className="h-5 w-5" aria-hidden />}
        </Button>
      </header>

      {/* Mobile drawer */}
      {menuOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          <button type="button" aria-label="Fechar menu" className="absolute inset-0 bg-black/60" onClick={close} />
          <aside
            id="menu-movel"
            role="dialog"
            aria-modal="true"
            aria-label="Menu"
            className="absolute inset-y-0 left-0 w-72 max-w-[85vw] overflow-y-auto border-r bg-background p-4 shadow-xl"
          >
            <div className="mb-4 flex items-center justify-between">
              <span className="font-semibold">ISCTRADE</span>
              <Button variant="ghost" size="sm" aria-label="Fechar menu" onClick={close} autoFocus>
                <X className="h-5 w-5" aria-hidden />
              </Button>
            </div>
            <SidebarContent {...sidebarProps} onNavigate={close} idSuffix="mobile" />
          </aside>
        </div>
      )}

      {/* Desktop sidebar */}
      <aside className="hidden w-60 shrink-0 border-r p-4 md:block">
        <Link href="/dashboard" className="mb-4 block font-semibold">
          ISCTRADE
        </Link>
        <SidebarContent {...sidebarProps} onNavigate={() => undefined} idSuffix="desktop" />
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <main id="conteudo" tabIndex={-1} className="flex-1 overflow-x-hidden p-4 focus:outline-none md:p-6">
          {!mfaBlocked && <TickerBar />}
          {mfaBlocked && !onSecurityPage ? (
            <Alert variant="warning" className="max-w-xl">
              <p className="mb-2 font-medium">Autenticação em dois fatores obrigatória</p>
              <p className="mb-3">
                {user.mfa_reason === "live"
                  ? "Você tem acesso a contas reais (live). Ative o 2FA para continuar usando a plataforma."
                  : "O administrador exige 2FA para todos os usuários. Ative-o para continuar."}
              </p>
              <Button asChild size="sm">
                <Link href={SECURITY_PATH}>Ativar 2FA</Link>
              </Button>
            </Alert>
          ) : (
            children
          )}
        </main>
        <LegalFooter />
      </div>
      <DisclaimerGate user={user} onAccepted={() => void reload()} />
    </div>
  );
}
