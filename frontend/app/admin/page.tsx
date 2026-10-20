"use client";

import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader, Pager } from "@/components/common";
import { AuditTable, LIMIT, usePaged } from "@/components/paged";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, errorMessage } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { AdminStats, AdminWorkspace, User } from "@/lib/types";

function UsersTab({ me }: { me: User }) {
  const [tick, setTick] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const { page, error: loadError, offset, go } = usePaged<User>((o) => api.adminUsers({ limit: LIMIT, offset: o }), [tick]);

  async function update(id: string, data: Parameters<typeof api.adminUpdateUser>[1]) {
    setError(null);
    try {
      await api.adminUpdateUser(id, data);
      setTick((t) => t + 1);
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  if (loadError) return <Alert variant="destructive">{loadError}</Alert>;
  if (!page) return <EmptyState>Carregando...</EmptyState>;
  return (
    <>
      {error && <Alert variant="destructive" className="mb-3">{error}</Alert>}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Usuário</TableHead>
            <TableHead>Papel global</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Cadastro</TableHead>
            <TableHead className="text-right">Ações</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {page.items.map((u) => (
            <TableRow key={u.id}>
              <TableCell>
                <div>{u.full_name ?? "—"}</div>
                <div className="text-xs text-muted-foreground">{u.email}</div>
              </TableCell>
              <TableCell>
                <Badge variant={u.role === "admin" ? "default" : "secondary"}>{u.role === "admin" ? "Super-admin" : "Usuário"}</Badge>
              </TableCell>
              <TableCell>
                <Badge variant={u.is_active ? "success" : "destructive"}>{u.is_active ? "Ativo" : "Bloqueado"}</Badge>
              </TableCell>
              <TableCell className="text-xs">{dateTime(u.created_at)}</TableCell>
              <TableCell className="space-x-1 whitespace-nowrap text-right">
                {u.id !== me.id && (
                  <>
                    <Button size="sm" variant="outline" onClick={() => update(u.id, { role: u.role === "admin" ? "trader" : "admin" })}>
                      {u.role === "admin" ? "Remover admin" : "Tornar admin"}
                    </Button>
                    <Button
                      size="sm"
                      variant={u.is_active ? "destructive" : "outline"}
                      onClick={() => update(u.id, { is_active: !u.is_active })}
                    >
                      {u.is_active ? "Bloquear" : "Desbloquear"}
                    </Button>
                  </>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Pager total={page.total} limit={LIMIT} offset={offset} onChange={go} />
    </>
  );
}

function WorkspacesTab() {
  const { page, error, offset, go } = usePaged<AdminWorkspace>((o) => api.adminWorkspaces({ limit: LIMIT, offset: o }), []);
  if (error) return <Alert variant="destructive">{error}</Alert>;
  if (!page) return <EmptyState>Carregando...</EmptyState>;
  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Workspace</TableHead>
            <TableHead>Proprietário</TableHead>
            <TableHead>Contas</TableHead>
            <TableHead>Em execução</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {page.items.map((w) => (
            <TableRow key={w.id}>
              <TableCell>{w.name}</TableCell>
              <TableCell>{w.owner_email}</TableCell>
              <TableCell>{w.accounts}</TableCell>
              <TableCell>{w.running_accounts}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Pager total={page.total} limit={LIMIT} offset={offset} onChange={go} />
    </>
  );
}

export default function AdminPage() {
  const { user } = useAuth();
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .adminStats()
      .then(setStats)
      .catch((e) => setError(errorMessage(e)));
  }, []);
  useEffect(load, [load]);

  if (!user) return null;
  if (user.role !== "admin") return <Alert variant="destructive">Acesso restrito a administradores.</Alert>;

  const ks = stats?.global_kill_switch;

  async function toggleKill() {
    if (!ks) return;
    const activating = !ks.active;
    if (activating && !confirm("Ativar o kill switch GLOBAL? Todas as contas serão paradas e novas ordens bloqueadas.")) return;
    try {
      await api.adminKillSwitch(activating, reason || undefined);
      setReason("");
      load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  const tiles: [string, string | number][] = stats
    ? [
        ["Usuários", stats.users],
        ["Workspaces", stats.workspaces],
        ["Contas", `${stats.accounts_running}/${stats.accounts} em execução`],
        ["Contas em kill switch", stats.accounts_killed],
        ["Bots", `${stats.bots_running}/${stats.bots} em execução`],
        ["Bots com erro", stats.bots_error],
        ["Ordens", stats.orders],
        ["Execuções", stats.trades],
      ]
    : [];

  return (
    <div className="space-y-6">
      <PageHeader title="Administração" description="Visão global da instalação (super-admin)." />
      {error && <Alert variant="destructive">{error}</Alert>}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {tiles.map(([label, value]) => (
          <Card key={label}>
            <CardHeader>
              <CardDescription>{label}</CardDescription>
              <CardTitle className="text-xl">{value}</CardTitle>
            </CardHeader>
          </Card>
        ))}
      </div>
      {ks && (
        <Card className={ks.active ? "border-destructive" : undefined}>
          <CardHeader>
            <CardTitle className="text-lg">Kill switch global</CardTitle>
            <CardDescription>
              {ks.active
                ? `ATIVO${ks.reason ? ` — ${ks.reason}` : ""}. Nenhuma ordem nova é aceita em nenhuma conta.`
                : "Inativo. Ao ativar, todas as contas são paradas e novas ordens são bloqueadas."}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-2">
            {!ks.active && (
              <Input className="w-72" placeholder="Motivo (opcional)" value={reason} onChange={(e) => setReason(e.target.value)} />
            )}
            <Button variant={ks.active ? "outline" : "destructive"} onClick={toggleKill}>
              {ks.active ? "Desativar kill switch" : "Ativar kill switch"}
            </Button>
          </CardContent>
        </Card>
      )}
      <Card>
        <CardContent className="pt-6">
          <Tabs defaultValue="users">
            <TabsList>
              <TabsTrigger value="users">Usuários</TabsTrigger>
              <TabsTrigger value="workspaces">Workspaces</TabsTrigger>
              <TabsTrigger value="audit">Auditoria global</TabsTrigger>
            </TabsList>
            <TabsContent value="users" className="pt-3">
              <UsersTab me={user} />
            </TabsContent>
            <TabsContent value="workspaces" className="pt-3">
              <WorkspacesTab />
            </TabsContent>
            <TabsContent value="audit" className="pt-3">
              <AuditTable fetcher={(o) => api.adminAudit({ limit: LIMIT, offset: o })} deps={[]} />
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>
    </div>
  );
}
