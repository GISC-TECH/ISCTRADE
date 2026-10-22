"use client";

import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader } from "@/components/common";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import type { Member, Role, Workspace } from "@/lib/types";

const ROLES: Role[] = ["admin", "trader", "viewer"];
const ROLE_LABEL: Record<Role, string> = { admin: "Administrador", trader: "Trader", viewer: "Leitura" };

function Members({ ws, canManage }: { ws: Workspace; canManage: boolean }) {
  const [members, setMembers] = useState<Member[] | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .listMembers(ws.id)
      .then(setMembers)
      .catch((e) => setError(errorMessage(e)));
  }, [ws.id]);
  useEffect(load, [load]);

  async function run(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">Membros de “{ws.name}”</CardTitle>
        <CardDescription>Administradores gerenciam contas e membros; traders operam; leitura apenas visualiza.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && <Alert variant="destructive">{error}</Alert>}
        {canManage && (
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              run(() => api.addMember(ws.id, email, role)).then(() => setEmail(""));
            }}
          >
            <div className="space-y-1">
              <Label htmlFor="m-email">E-mail do usuário cadastrado</Label>
              <Input id="m-email" type="email" required className="w-64" value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="m-role">Papel</Label>
              <Select id="m-role" value={role} onChange={(e) => setRole(e.target.value as Role)} className="w-40">
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {ROLE_LABEL[r]}
                  </option>
                ))}
              </Select>
            </div>
            <Button type="submit">Adicionar</Button>
          </form>
        )}
        {members === null ? (
          <EmptyState>Carregando...</EmptyState>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Usuário</TableHead>
                <TableHead>Papel</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {members.map((m) => (
                <TableRow key={m.user_id}>
                  <TableCell>
                    <div>{m.full_name ?? m.email}</div>
                    <div className="text-xs text-muted-foreground">{m.email}</div>
                  </TableCell>
                  <TableCell>
                    {m.is_owner ? (
                      <Badge>Proprietário</Badge>
                    ) : canManage ? (
                      <Select
                        aria-label={`Papel de ${m.email}`}
                        className="h-8 w-40"
                        value={m.role}
                        onChange={(e) => run(() => api.updateMember(ws.id, m.user_id, e.target.value as Role))}
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>
                            {ROLE_LABEL[r]}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      ROLE_LABEL[m.role]
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    {canManage && !m.is_owner && (
                      <Button
                        size="sm"
                        variant="destructive"
                        onClick={() => confirm(`Remover ${m.email}?`) && run(() => api.removeMember(ws.id, m.user_id))}
                      >
                        Remover
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

export default function WorkspacesPage() {
  const { user, workspaces, workspace, reload, switchWorkspace } = useAuth();
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const current = workspaces.find((w) => w.id === (selected ?? workspace?.id)) ?? null;

  async function run(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      await reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader title="Workspaces" description="Cada workspace isola contas, bots, histórico e membros." />
      {error && <Alert variant="destructive">{error}</Alert>}
      <Card>
        <CardContent className="space-y-4 pt-6">
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              run(() => api.createWorkspace(name)).then(() => setName(""));
            }}
          >
            <div className="space-y-1">
              <Label htmlFor="ws-name">Novo workspace</Label>
              <Input id="ws-name" required className="w-64" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <Button type="submit">Criar</Button>
          </form>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Nome</TableHead>
                <TableHead>Seu papel</TableHead>
                <TableHead className="text-right">Ações</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {workspaces.map((w) => (
                <TableRow key={w.id}>
                  <TableCell>
                    {w.name} {w.id === workspace?.id && <Badge variant="success">Ativo</Badge>}
                  </TableCell>
                  <TableCell>{w.owner_id === user?.id ? "Proprietário" : w.role ? ROLE_LABEL[w.role] : "—"}</TableCell>
                  <TableCell className="space-x-1 whitespace-nowrap text-right">
                    {w.id !== workspace?.id && (
                      <Button size="sm" onClick={() => switchWorkspace(w.id).then(() => window.location.reload())}>
                        Ativar
                      </Button>
                    )}
                    <Button size="sm" variant="outline" onClick={() => setSelected(w.id)}>
                      Membros
                    </Button>
                    {w.role === "admin" && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => {
                          const n = prompt("Novo nome", w.name);
                          if (n && n !== w.name) run(() => api.renameWorkspace(w.id, n));
                        }}
                      >
                        Renomear
                      </Button>
                    )}
                    {w.owner_id === user?.id && workspaces.length > 1 && (
                      <Button
                        size="sm"
                        variant="destructive"
                        onClick={() =>
                          confirm(`Excluir o workspace "${w.name}" e todos os seus dados?`) &&
                          run(() => api.deleteWorkspace(w.id))
                        }
                      >
                        Excluir
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      {current && <Members key={current.id} ws={current} canManage={current.role === "admin"} />}
    </div>
  );
}
