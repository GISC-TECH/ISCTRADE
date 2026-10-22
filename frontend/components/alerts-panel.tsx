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
import type { AlertChannelStatus, AlertItem, AlertSettings, AlertSeverity, AlertStatus } from "@/lib/types";

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
  telegram_bot_api: "Telegram Bot API",
  smtp: "SMTP",
};

export const CHANNELS: Record<string, { label: string; defaultsEnv: string; setupEnv: string }> = {
  whatsapp: {
    label: "WhatsApp",
    defaultsEnv: "WHATSAPP_TO",
    setupEnv: "WHATSAPP_ENABLED, WHATSAPP_PROVIDER e WHATSAPP_API_TOKEN",
  },
  telegram: { label: "Telegram", defaultsEnv: "TELEGRAM_CHAT_IDS", setupEnv: "TELEGRAM_ENABLED e TELEGRAM_BOT_TOKEN" },
  email: { label: "E-mail", defaultsEnv: "EMAIL_TO", setupEnv: "EMAIL_ENABLED e SMTP_HOST/SMTP_USER/SMTP_PASSWORD" },
};

export function channelList(s: { channel: AlertChannelStatus; channels?: AlertChannelStatus[] }): AlertChannelStatus[] {
  return s.channels?.length ? s.channels : [s.channel];
}

export function ChannelBadge({ ch }: { ch: AlertChannelStatus }) {
  return ch.enabled && ch.configured ? (
    <Badge variant="success">configurado</Badge>
  ) : (
    <Badge variant={ch.enabled ? "warning" : "secondary"}>{ch.enabled ? "credenciais ausentes" : "desativado"}</Badge>
  );
}

/** Server-side channel status (WhatsApp, Telegram, e-mail). No secrets are ever returned by the API. */
export function ChannelsCard({ channels }: { channels: AlertChannelStatus[] }) {
  const none = !channels.some((c) => c.enabled && c.configured);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Canais do servidor</CardTitle>
        <CardDescription>Credenciais ficam no .env do servidor; aqui você define os destinatários do workspace.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="grid gap-3 md:grid-cols-3">
          {channels.map((ch) => {
            const info = CHANNELS[ch.channel];
            return (
              <div key={ch.channel} className="space-y-1 rounded-md border p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{info?.label ?? ch.channel}</span>
                  <ChannelBadge ch={ch} />
                </div>
                <p className="text-xs text-muted-foreground">
                  Provedor: {PROVIDERS[ch.provider] ?? ch.provider}
                  <br />
                  Destinatários padrão ({info?.defaultsEnv ?? "servidor"}): {ch.default_recipients}
                </p>
                {!(ch.enabled && ch.configured) && info && (
                  <p className="text-xs text-muted-foreground">Para ativar: {info.setupEnv} (veja docs/ALERTS.md).</p>
                )}
              </div>
            );
          })}
        </div>
        {none && (
          <p className="text-xs text-muted-foreground">
            Nenhum canal configurado: os alertas ficam registrados apenas no histórico abaixo.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

const splitList = (raw: string) =>
  raw
    .split(/[\n,;]+/)
    .map((r) => r.trim())
    .filter(Boolean);

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
  const [chats, setChats] = useState((settings.telegram_chat_ids ?? []).join("\n"));
  const [emails, setEmails] = useState((settings.emails ?? []).join("\n"));
  const multiChannel = settings.telegram_chat_ids !== undefined;
  const [events, setEvents] = useState<Record<string, boolean>>(settings.events);
  const [minSeverity, setMinSeverity] = useState<AlertSeverity>(settings.min_severity);
  const [throttle, setThrottle] = useState(String(settings.throttle_minutes));
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const saved = await api.updateAlertSettings({
        enabled,
        recipients: splitList(recipients),
        ...(multiChannel ? { telegram_chat_ids: splitList(chats), emails: splitList(emails) } : {}),
        events,
        min_severity: minSeverity,
        throttle_minutes: Number(throttle) || 0,
      });
      onSaved(saved);
      setRecipients(saved.recipients.join("\n"));
      setChats((saved.telegram_chat_ids ?? []).join("\n"));
      setEmails((saved.emails ?? []).join("\n"));
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
            Destinatários, eventos e limites de envio.{" "}
            {canManage ? "" : "Somente administradores do workspace podem alterar (destinatários aparecem mascarados)."}
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
          <RecipientsField
            id="recipients"
            label="WhatsApp (E.164, um por linha)"
            placeholder="+5511999999999"
            value={recipients}
            onChange={setRecipients}
            disabled={!canManage}
            hint="Vazio: usa os destinatários padrão do servidor (WHATSAPP_TO)."
          />
          {multiChannel && (
            <>
              <RecipientsField
                id="telegram-chats"
                label="Telegram (chat IDs, um por linha)"
                placeholder="123456789 ou -1001234567890"
                value={chats}
                onChange={setChats}
                disabled={!canManage}
                hint="Envie /start ao bot antes. Vazio: usa TELEGRAM_CHAT_IDS do servidor."
              />
              <RecipientsField
                id="alert-emails"
                label="E-mails (um por linha)"
                placeholder="risco@empresa.com.br"
                value={emails}
                onChange={setEmails}
                disabled={!canManage}
                hint="Vazio: usa EMAIL_TO do servidor."
              />
            </>
          )}
        </div>

        <div className="grid gap-4 md:grid-cols-3">
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

function RecipientsField({
  id,
  label,
  placeholder,
  value,
  onChange,
  disabled,
  hint,
}: {
  id: string;
  label: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  hint: string;
}) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <textarea
        id={id}
        aria-describedby={`${id}-hint`}
        className="min-h-[96px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
        placeholder={placeholder}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        {hint} Máximo de 10.
      </p>
    </div>
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

