"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ChannelBadge, channelList, CHANNELS, PROVIDERS, RecentAlerts } from "@/components/alerts-panel";
import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader } from "@/components/common";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, errorMessage } from "@/lib/api";
import type { MonitoringStatus } from "@/lib/types";

const CHECK_LABELS: Record<string, string> = {
  database: "Banco de dados",
  redis: "Redis",
  broker: "Fila (Celery broker)",
  streams: "Streams (WebSocket)",
};

const READINESS: Record<string, { label: string; variant: "success" | "warning" | "destructive" }> = {
  ready: { label: "Operacional", variant: "success" },
  degraded: { label: "Degradado", variant: "warning" },
  not_ready: { label: "Indisponível", variant: "destructive" },
};

function HealthCard({ status, onRefresh }: { status: MonitoringStatus | null; onRefresh: () => void }) {
  if (!status) return <EmptyState>Carregando status...</EmptyState>;
  const r = READINESS[status.readiness.status] ?? READINESS.not_ready;
  return (
    <div className="grid gap-4 md:grid-cols-3">
      <Card className="md:col-span-2">
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <div>
            <CardTitle>Saúde do sistema</CardTitle>
            <CardDescription>Mesmas verificações do endpoint /health/ready.</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant={r.variant}>{r.label}</Badge>
            <Button size="sm" variant="outline" onClick={onRefresh}>
              Atualizar
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid gap-2 sm:grid-cols-2">
            {Object.entries(status.readiness.checks).map(([key, c]) => (
              <div key={key} className="flex items-center justify-between rounded-md border p-3 text-sm">
                <span>{CHECK_LABELS[key] ?? key}</span>
                <span className="flex items-center gap-2 text-xs text-muted-foreground">
                  {c.latency_ms !== undefined && `${c.latency_ms} ms`}
                  {c.age_seconds !== undefined && c.age_seconds !== null && `há ${Math.round(c.age_seconds)}s`}
                  {c.error && c.error}
                  {c.skipped && c.skipped}
                  <Badge variant={c.ok ? "success" : key === "streams" ? "warning" : "destructive"}>
                    {c.ok ? "ok" : (c.state ?? "falha")}
                  </Badge>
                </span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Canais de alertas</CardTitle>
          <CardDescription>Status no servidor e envios nas últimas 24h.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {channelList(status).map((ch) => (
            <div key={ch.channel} className="flex items-center justify-between gap-2">
              <span>
                {CHANNELS[ch.channel]?.label ?? ch.channel}
                <span className="text-xs text-muted-foreground"> · {PROVIDERS[ch.provider] ?? ch.provider}</span>
              </span>
              <ChannelBadge ch={ch} />
            </div>
          ))}
          <div className="flex justify-between border-t pt-2">
            <span>Alertas (24h)</span>
            <span>{status.alerts_24h}</span>
          </div>
          <div className="flex justify-between">
            <span>Falhas de envio (24h)</span>
            <span className={status.alerts_failed_24h ? "text-red-500" : ""}>{status.alerts_failed_24h}</span>
          </div>
          {!channelList(status).some((c) => c.enabled && c.configured) && (
            <p className="text-xs text-muted-foreground">
              Nenhum canal configurado: os alertas ficam só no histórico. Veja docs/ALERTS.md.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export default function MonitoringPage() {
  const { workspace } = useAuth();
  const [status, setStatus] = useState<MonitoringStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const fetchStatus = useCallback(
    () =>
      api
        .monitoringStatus()
        .then(setStatus)
        .catch((e) => setError(errorMessage(e))),
    []
  );

  useEffect(() => {
    void fetchStatus();
    const t = setInterval(() => api.monitoringStatus().then(setStatus).catch(() => undefined), 30_000);
    return () => clearInterval(t);
  }, [fetchStatus, workspace?.id]);

  return (
    <div className="space-y-6">
      <PageHeader title="Monitoramento" description="Saúde dos serviços e alertas recentes.">
        <Button asChild variant="outline">
          <Link href="/dashboard/alerts">Configurar alertas</Link>
        </Button>
      </PageHeader>
      {error && <Alert variant="destructive">{error}</Alert>}
      <HealthCard
        status={status}
        onRefresh={() => {
          void fetchStatus();
          setRefreshKey((k) => k + 1);
        }}
      />
      <Card>
        <CardHeader>
          <CardTitle>Alertas recentes</CardTitle>
          <CardDescription>
            Inclui alertas globais (kill switch global, falhas de tarefas, streams). Destinatários, eventos e filtros
            ficam em <Link className="underline" href="/dashboard/alerts">Alertas</Link>.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <RecentAlerts refreshKey={refreshKey} />
        </CardContent>
      </Card>
    </div>
  );
}
