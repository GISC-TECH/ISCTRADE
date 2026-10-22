"use client";

import { useState } from "react";

import { EmptyState, Pager } from "@/components/common";
import { LIMIT, usePaged } from "@/components/paged";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { AlertItem, AlertSettings, AlertSeverity, AlertStatus } from "@/lib/types";

export const SEVERITY: Record<AlertSeverity, { label: string; variant: "secondary" | "warning" | "destructive" }> = {
  info: { label: "info", variant: "secondary" },
  warning: { label: "aviso", variant: "warning" },
  critical: { label: "crítico", variant: "destructive" },
};

export const STATUS: Record<AlertStatus, { label: string; variant: "success" | "secondary" | "warning" | "destructive" }> = {
  sent: { label: "enviado", variant: "success" },
  pending: { label: "pendente", variant: "secondary" },
  partial: { label: "parcial", variant: "warning" },
  failed: { label: "falhou", variant: "destructive" },
  disabled: { label: "sem canal", variant: "secondary" },
};

export const PROVIDERS: Record<string, string> = {
  wago: "WAGO (hospedado)",
  wago_api: "wago-api (self-hosted)",
  meta_cloud: "WhatsApp Cloud API (Meta)",
  evolution: "Evolution API",
  generic_http: "Gateway HTTP genérico",
};

export function SettingsCard({
  settings,
  canManage,
  onSaved,
  onTested,
}: {
  settings: AlertSettings;
  canManage: boolean;
  onSaved: (s: AlertSettings) => void;
  onTested: () => void;
}) {
  const [enabled, setEnabled] = useState(settings.enabled);
  const [recipients, setRecipients] = useState(settings.recipients.join("\n"));
  const [events, setEvents] = useState<Record<string, boolean>>(settings.events);
  const [minSeverity, setMinSeverity] = useState<AlertSeverity>(settings.min_severity);
  const [throttle, setThrottle] = useState(String(settings.throttle_minutes));
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const list = recipients
        .split(/[\n,;]+/)
        .map((r) => r.trim())
        .filter(Boolean);
      const saved = await api.updateAlertSettings({
        enabled,
        recipients: list,
        events,
        min_severity: minSeverity,
        throttle_minutes: Number(throttle) || 0,
      });
      onSaved(saved);
      setRecipients(saved.recipients.join("\n"));
      setMsg({ ok: true, text: "Configurações salvas." });
    } catch (e) {
      setMsg({ ok: false, text: errorMessage(e) });
    } finally {
      setBusy(false);
    }
  };

  const test = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const a = await api.sendTestAlert();
      onTested();
      const st = STATUS[a.status];
      setMsg({
        ok: a.status === "sent",
        text:
          a.status === "sent"
            ? `Alerta de teste enviado para ${a.recipients.join(", ")}.`
            : `Alerta de teste ${st?.label ?? a.status}${a.error ? `: ${a.error}` : ""}`,
      });
    } catch (e) {
      setMsg({ ok: false, text: errorMessage(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between space-y-0">
        <div>
          <CardTitle>Alertas do workspace</CardTitle>
          <CardDescription>
            Eventos enviados por WhatsApp. {canManage ? "" : "Somente administradores do workspace podem alterar."}
          </CardDescription>
        </div>
        {canManage && (
          <Button onClick={test} disabled={busy} variant="outline">
            Enviar alerta de teste
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-5">
        {msg && <Alert variant={msg.ok ? "success" : "destructive"}>{msg.text}</Alert>}
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={enabled}
            disabled={!canManage}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Alertas ativados para este workspace
        </label>

        <div className="grid gap-4 md:grid-cols-3">
          <div className="space-y-1 md:col-span-1">
            <Label htmlFor="recipients">Números do WhatsApp (E.164, um por linha)</Label>
            <textarea
              id="recipients"
              className="min-h-[96px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              placeholder="+5511999999999"
              value={recipients}
              disabled={!canManage}
              onChange={(e) => setRecipients(e.target.value)}
            />
            {settings.uses_default_recipients && (
              <p className="text-xs text-muted-foreground">
                Vazio: usa os destinatários padrão do servidor (WHATSAPP_TO).
              </p>
            )}
          </div>
          <div className="space-y-1">
            <Label htmlFor="severity">Severidade mínima</Label>
            <Select
              id="severity"
              value={minSeverity}
              disabled={!canManage}
              onChange={(e) => setMinSeverity(e.target.value as AlertSeverity)}
            >
              <option value="info">Info (todos)</option>
              <option value="warning">Aviso e crítico</option>
              <option value="critical">Somente crítico</option>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="throttle">Antispam (minutos por evento)</Label>
            <Input
              id="throttle"
              type="number"
              min={0}
              max={1440}
              value={throttle}
              disabled={!canManage}
              onChange={(e) => setThrottle(e.target.value)}
            />
          </div>
        </div>

        <div>
          <Label>Eventos</Label>
          <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {settings.available_events.map((ev) => (
              <label key={ev.key} className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm">
                <span className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={events[ev.key] ?? ev.default_enabled}
                    disabled={!canManage}
                    onChange={(e) => setEvents({ ...events, [ev.key]: e.target.checked })}
                  />
                  {ev.label}
                </span>
                <Badge variant={SEVERITY[ev.severity].variant}>{SEVERITY[ev.severity].label}</Badge>
              </label>
            ))}
          </div>
        </div>

        {canManage && (
          <Button onClick={save} disabled={busy}>
            Salvar configurações
          </Button>
        )}
      </CardContent>
    </Card>
  );
}

export function RecentAlerts({
  refreshKey,
  event,
  status,
}: {
  refreshKey: number;
  event?: string;
  status?: string;
}) {
  const { page, error, offset, go } = usePaged<AlertItem>(
    (o) => api.alerts({ limit: LIMIT, offset: o, event: event || undefined, status: status || undefined }),
    [refreshKey, event, status]
  );
  const [open, setOpen] = useState<string | null>(null);
  if (error) return <Alert variant="destructive">{error}</Alert>;
  if (!page) return <EmptyState>Carregando...</EmptyState>;
  if (!page.items.length) return <EmptyState>Nenhum alerta registrado.</EmptyState>;
  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Data</TableHead>
            <TableHead>Severidade</TableHead>
            <TableHead>Alerta</TableHead>
            <TableHead>Envio</TableHead>
            <TableHead>Destinatários</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {page.items.map((a) => (
            <TableRow
              key={a.id}
              className="cursor-pointer"
              tabIndex={0}
              aria-expanded={open === a.id}
              onClick={() => setOpen(open === a.id ? null : a.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setOpen(open === a.id ? null : a.id);
                }
              }}
            >
              <TableCell className="whitespace-nowrap text-xs">{dateTime(a.created_at)}</TableCell>
              <TableCell>
                <Badge variant={SEVERITY[a.severity]?.variant ?? "secondary"}>
                  {SEVERITY[a.severity]?.label ?? a.severity}
                </Badge>
              </TableCell>
              <TableCell className="max-w-md">
                <div className="font-medium">{a.title}</div>
                {open === a.id && (
                  <pre className="mt-2 whitespace-pre-wrap text-xs text-muted-foreground">{a.message}</pre>
                )}
              </TableCell>
              <TableCell>
                <Badge variant={STATUS[a.status]?.variant ?? "secondary"} title={a.error ?? ""}>
                  {STATUS[a.status]?.label ?? a.status}
                </Badge>
                {a.error && open === a.id && <div className="mt-1 text-xs text-red-500">{a.error}</div>}
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">{a.recipients.join(", ") || "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Pager total={page.total} limit={LIMIT} offset={offset} onChange={go} />
    </>
  );
}

