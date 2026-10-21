"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader } from "@/components/common";
import { ImpactBadge } from "@/components/sentiment";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import type { EconomicEvent } from "@/lib/types";

function relative(ms: number): string {
  const abs = Math.abs(ms);
  const m = Math.round(abs / 60_000);
  const text = m < 60 ? `${m} min` : m < 1440 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.floor(m / 1440)} d`;
  return ms >= 0 ? `em ${text}` : `há ${text}`;
}

export default function CalendarPage() {
  const { workspace } = useAuth();
  const [events, setEvents] = useState<EconomicEvent[] | null>(null);
  const [impact, setImpact] = useState("high");
  const [country, setCountry] = useState("USD");
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const canTrade = workspace?.role === "admin" || workspace?.role === "trader";

  const load = useCallback(() => {
    api
      .calendar({ impact: impact || undefined, country: country || undefined, days_back: 1, days_ahead: 7 })
      .then((e) => {
        setEvents(e);
        setError(null);
      })
      .catch((e) => setError(errorMessage(e)));
  }, [impact, country]);

  useEffect(() => {
    load();
    const t = setInterval(() => {
      setNow(Date.now());
      load();
    }, 60_000);
    return () => clearInterval(t);
  }, [load, workspace?.id]);

  const next = useMemo(
    () => events?.find((e) => e.impact === "high" && new Date(e.event_time).getTime() > now) ?? null,
    [events, now]
  );

  async function refresh() {
    setInfo(null);
    try {
      const r = await api.refreshCalendar();
      setInfo(r.queued ? "Atualização agendada." : "Fila indisponível no momento.");
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Calendário econômico"
        description="Eventos macro (ForexFactory + eventos manuais). Bots com pausa por evento não abrem novas posições na janela configurada."
      >
        {canTrade && (
          <Button variant="outline" onClick={refresh}>
            Atualizar
          </Button>
        )}
      </PageHeader>
      {error && <Alert variant="destructive">{error}</Alert>}
      {info && <Alert>{info}</Alert>}
      {next && (
        <Alert variant="warning">
          Próximo evento de alto impacto: <b>{next.title}</b> ({next.country}) —{" "}
          {new Date(next.event_time).toLocaleString("pt-BR")} ({relative(new Date(next.event_time).getTime() - now)}).
        </Alert>
      )}
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor="c-impact">Impacto</Label>
              <Select id="c-impact" value={impact} onChange={(e) => setImpact(e.target.value)}>
                <option value="">Todos</option>
                <option value="high">Alto</option>
                <option value="medium">Médio</option>
                <option value="low">Baixo</option>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="c-country">Moeda/país</Label>
              <Input
                id="c-country"
                placeholder="USD"
                value={country}
                onChange={(e) => setCountry(e.target.value.toUpperCase().trim())}
              />
            </div>
          </div>
          {events === null ? (
            <EmptyState>Carregando...</EmptyState>
          ) : events.length === 0 ? (
            <EmptyState>Nenhum evento no período (a tarefa calendar.refresh roda a cada hora).</EmptyState>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Horário</TableHead>
                  <TableHead>Evento</TableHead>
                  <TableHead>Moeda</TableHead>
                  <TableHead>Impacto</TableHead>
                  <TableHead>Projeção</TableHead>
                  <TableHead>Anterior</TableHead>
                  <TableHead>Fonte</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {events.map((e) => {
                  const t = new Date(e.event_time).getTime();
                  return (
                    <TableRow key={e.id} className={t < now ? "opacity-60" : ""}>
                      <TableCell className="whitespace-nowrap">
                        {new Date(e.event_time).toLocaleString("pt-BR")}
                        <div className="text-xs text-muted-foreground">{relative(t - now)}</div>
                      </TableCell>
                      <TableCell className="font-medium">{e.title}</TableCell>
                      <TableCell>{e.country}</TableCell>
                      <TableCell>
                        <ImpactBadge impact={e.impact} />
                      </TableCell>
                      <TableCell>{e.forecast ?? "—"}</TableCell>
                      <TableCell>{e.previous ?? "—"}</TableCell>
                      <TableCell className="text-xs">{e.source}</TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
