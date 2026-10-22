"use client";

import { useEffect, useState } from "react";

import { EmptyState } from "@/components/common";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { InviteCreated, InviteStatus, RegistrationInvite, RegistrationMode } from "@/lib/types";

const STATUS: Record<InviteStatus, { label: string; variant: "success" | "secondary" | "destructive" | "default" }> = {
  pending: { label: "Pendente", variant: "default" },
  used: { label: "Utilizado", variant: "success" },
  expired: { label: "Expirado", variant: "secondary" },
  revoked: { label: "Revogado", variant: "destructive" },
};

const MODE_NOTICE: Record<RegistrationMode, string | null> = {
  invite: null,
  open: "O cadastro está aberto (REGISTRATION_MODE=open): qualquer pessoa pode se cadastrar e o convite é opcional.",
  closed:
    "O cadastro está fechado (REGISTRATION_MODE=closed): convites só passam a valer com REGISTRATION_MODE=invite no backend.",
};

/** Link the invitee opens; the signup page reads `?invite=` and fills the code. */
export function inviteLink(token: string, origin: string = window.location.origin): string {
  return `${origin}/auth/register?invite=${encodeURIComponent(token)}`;
}

/** Super-admin: create, list and revoke single-use registration invites (REGISTRATION_MODE=invite). */
export function InvitesTab() {
  const [items, setItems] = useState<RegistrationInvite[] | null>(null);
  const [mode, setMode] = useState<RegistrationMode | null>(null);
  const [tick, setTick] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [note, setNote] = useState("");
  const [hours, setHours] = useState("72");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<InviteCreated | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .adminInvites()
      .then((rows) => alive && setItems(rows))
      .catch((e) => alive && setError(errorMessage(e)));
    api
      .registrationMode()
      .then((r) => alive && setMode(r.mode))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [tick]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const h = Number(hours);
      const res = await api.adminCreateInvite({
        email: email.trim() || undefined,
        note: note.trim() || undefined,
        hours: Number.isFinite(h) && h > 0 ? Math.round(h) : undefined,
      });
      setCreated(res);
      setCopied(false);
      setEmail("");
      setNote("");
      setTick((t) => t + 1);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(inv: RegistrationInvite) {
    if (!confirm(`Revogar o convite${inv.email ? ` para ${inv.email}` : ""}? O link deixa de funcionar.`)) return;
    setError(null);
    try {
      await api.adminRevokeInvite(inv.id);
      setTick((t) => t + 1);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  const notice = mode ? MODE_NOTICE[mode] : null;
  const link = created ? inviteLink(created.token) : "";

  return (
    <div className="space-y-4">
      {notice && <Alert variant={mode === "closed" ? "warning" : "default"}>{notice}</Alert>}
      <form onSubmit={create} className="grid gap-3 sm:grid-cols-[2fr_2fr_1fr_auto] sm:items-end">
        <div className="space-y-1">
          <Label htmlFor="invite-email">E-mail (opcional)</Label>
          <Input
            id="invite-email"
            type="email"
            placeholder="convite vale só para este e-mail"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="invite-note">Observação (opcional)</Label>
          <Input id="invite-note" maxLength={255} value={note} onChange={(e) => setNote(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="invite-hours">Validade (horas)</Label>
          <Input
            id="invite-hours"
            type="number"
            min={1}
            max={2160}
            value={hours}
            onChange={(e) => setHours(e.target.value)}
          />
        </div>
        <Button type="submit" disabled={busy}>
          {busy ? "Gerando..." : "Gerar convite"}
        </Button>
      </form>

      {created && (
        <Alert variant="success" role="status">
          <div className="space-y-2">
            <p className="font-medium">
              Convite criado{created.email ? ` para ${created.email}` : ""}. Copie o link agora: ele não será exibido de
              novo.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Input readOnly aria-label="Link do convite" value={link} className="font-mono text-xs" onFocus={(e) => e.target.select()} />
              <Button type="button" size="sm" variant="outline" onClick={() => copy(link)}>
                {copied ? "Copiado" : "Copiar link"}
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setCreated(null)}>
                Fechar
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              Uso único, expira em {dateTime(created.expires_at)}. Envie por um canal seguro.
            </p>
          </div>
        </Alert>
      )}

      {error && <Alert variant="destructive">{error}</Alert>}

      {!items ? (
        <EmptyState>Carregando...</EmptyState>
      ) : items.length === 0 ? (
        <EmptyState>Nenhum convite criado.</EmptyState>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>E-mail</TableHead>
              <TableHead>Observação</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Criado</TableHead>
              <TableHead>Expira / usado</TableHead>
              <TableHead className="text-right">Ações</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((inv) => (
              <TableRow key={inv.id}>
                <TableCell>{inv.email ?? <span className="text-muted-foreground">qualquer e-mail</span>}</TableCell>
                <TableCell className="max-w-[16rem] truncate">{inv.note ?? "—"}</TableCell>
                <TableCell>
                  <Badge variant={STATUS[inv.status].variant}>{STATUS[inv.status].label}</Badge>
                </TableCell>
                <TableCell className="text-xs">{dateTime(inv.created_at)}</TableCell>
                <TableCell className="text-xs">{dateTime(inv.used_at ?? inv.expires_at)}</TableCell>
                <TableCell className="text-right">
                  {inv.status === "pending" && (
                    <Button size="sm" variant="outline" onClick={() => revoke(inv)} aria-label={`Revogar convite ${inv.email ?? inv.id}`}>
                      Revogar
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
