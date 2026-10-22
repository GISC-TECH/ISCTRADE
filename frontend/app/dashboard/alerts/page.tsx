"use client";

import { useEffect, useState } from "react";

import { RecentAlerts, SettingsCard, STATUS } from "@/components/alerts-panel";
import { useAuth } from "@/components/auth-provider";
import { PageHeader } from "@/components/common";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorMessage } from "@/lib/api";
import type { AlertSettings } from "@/lib/types";

export default function AlertsPage() {
  const { workspace } = useAuth();
  const [settings, setSettings] = useState<AlertSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [event, setEvent] = useState("");
  const [status, setStatus] = useState("");
  const canManage = workspace?.role === "admin";

  useEffect(() => {
    api
      .alertSettings()
      .then(setSettings)
      .catch((e) => setError(errorMessage(e)));
  }, [workspace?.id]);

  const ch = settings?.channel;

  return (
    <div className="space-y-6">
      <PageHeader title="Alertas" description="Destinatários do WhatsApp, eventos, severidade mínima e histórico." />
      {error && <Alert variant="destructive">{error}</Alert>}
      {ch && (
        <Card>
          <CardContent className="flex flex-wrap items-center gap-3 pt-6 text-sm">
            <span>Canal WhatsApp do servidor:</span>
            {ch.enabled && ch.configured ? (
              <Badge variant="success">configurado</Badge>
            ) : (
              <Badge variant="warning">{ch.enabled ? "credenciais ausentes" : "desativado"}</Badge>
            )}
            <span className="text-muted-foreground">
              Provedor: {ch.provider} · destinatários padrão (WHATSAPP_TO): {ch.default_recipients}
            </span>
            {!(ch.enabled && ch.configured) && (
              <span className="w-full text-xs text-muted-foreground">
                Sem canal configurado os alertas ficam só no histórico. Configure WHATSAPP_ENABLED, WHATSAPP_PROVIDER e
                WHATSAPP_API_TOKEN no .env do servidor (veja docs/ALERTS.md).
              </span>
            )}
          </CardContent>
        </Card>
      )}
      {settings && (
        <SettingsCard
          key={workspace?.id ?? "ws"}
          settings={settings}
          canManage={canManage}
          onSaved={(s) => {
            setSettings(s);
            setRefreshKey((k) => k + 1);
          }}
          onTested={() => setRefreshKey((k) => k + 1)}
        />
      )}
      <Card>
        <CardHeader>
          <CardTitle>Histórico de alertas</CardTitle>
          <CardDescription>Inclui alertas globais (kill switch global, falhas de tarefas, streams).</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap gap-3">
            <div className="space-y-1">
              <Label htmlFor="al-event">Evento</Label>
              <Select id="al-event" value={event} onChange={(e) => setEvent(e.target.value)} className="w-64">
                <option value="">Todos</option>
                {settings?.available_events.map((ev) => (
                  <option key={ev.key} value={ev.key}>
                    {ev.label}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="al-status">Envio</Label>
              <Select id="al-status" value={status} onChange={(e) => setStatus(e.target.value)} className="w-44">
                <option value="">Todos</option>
                {Object.entries(STATUS).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v.label}
                  </option>
                ))}
              </Select>
            </div>
          </div>
          <RecentAlerts refreshKey={refreshKey} event={event} status={status} />
        </CardContent>
      </Card>
    </div>
  );
}
