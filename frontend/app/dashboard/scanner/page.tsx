"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader } from "@/components/common";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { ScannerResponse, ScannerRow } from "@/lib/types";

type SortKey = keyof Pick<
  ScannerRow,
  | "score"
  | "quote_volume"
  | "change_pct_24h"
  | "atr_pct"
  | "adx"
  | "momentum_pct"
  | "volume_spike"
  | "funding_rate_pct"
  | "rsi"
>;

const COLUMNS: { key: SortKey; label: string; title: string }[] = [
  { key: "score", label: "Score", title: "Pontuação ponderada 0–100" },
  { key: "change_pct_24h", label: "24h %", title: "Variação em 24h" },
  { key: "quote_volume", label: "Volume 24h", title: "Volume em USDT nas últimas 24h" },
  { key: "adx", label: "ADX", title: "Força da tendência (ADX 14)" },
  { key: "atr_pct", label: "ATR %", title: "Volatilidade: ATR(14) / preço" },
  { key: "momentum_pct", label: "Momentum %", title: "Variação nos últimos 20 candles" },
  { key: "volume_spike", label: "Vol. spike", title: "Volume do último candle / média de 20" },
  { key: "funding_rate_pct", label: "Funding %", title: "Última taxa de funding" },
  { key: "rsi", label: "RSI", title: "RSI(14)" },
];

const compact = new Intl.NumberFormat("pt-BR", { notation: "compact", maximumFractionDigits: 1 });
const num = (v: number | null, d = 2) =>
  v === null || v === undefined ? "—" : v.toLocaleString("pt-BR", { maximumFractionDigits: d });

const TREND: Record<string, { label: string; variant: "success" | "destructive" | "secondary" }> = {
  up: { label: "alta", variant: "success" },
  down: { label: "baixa", variant: "destructive" },
  flat: { label: "lateral", variant: "secondary" },
};

export default function ScannerPage() {
  const { workspace } = useAuth();
  const [data, setData] = useState<ScannerResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [sort, setSort] = useState<SortKey>("score");
  const [desc, setDesc] = useState(true);
  const [trend, setTrend] = useState("");
  const [signal, setSignal] = useState("");
  const [minVolume, setMinVolume] = useState("");
  const [minScore, setMinScore] = useState("");
  const [search, setSearch] = useState("");
  const canTrade = workspace?.role === "admin" || workspace?.role === "trader";

  const load = useCallback(() => {
    api
      .scanner({
        trend: trend || undefined,
        signal: signal || undefined,
        min_quote_volume: minVolume ? Number(minVolume) * 1_000_000 : undefined,
        min_score: minScore ? Number(minScore) : undefined,
        limit: 500,
      })
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(errorMessage(e)));
  }, [trend, signal, minVolume, minScore]);

  useEffect(() => {
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load, workspace?.id]);

  const rows = useMemo(() => {
    const list = (data?.results ?? []).filter((r) => !search || r.symbol.includes(search.toUpperCase()));
    return [...list].sort((a, b) => {
      const av = a[sort];
      const bv = b[sort];
      if (av === null) return 1;
      if (bv === null) return -1;
      return desc ? (bv as number) - (av as number) : (av as number) - (bv as number);
    });
  }, [data, sort, desc, search]);

  function toggleSort(key: SortKey) {
    if (key === sort) setDesc(!desc);
    else {
      setSort(key);
      setDesc(true);
    }
  }

  async function refresh() {
    setInfo(null);
    try {
      const r = await api.refreshScanner();
      setInfo(r.queued ? "Nova varredura agendada; a tabela atualiza em instantes." : "Fila indisponível no momento.");
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Scanner de mercado"
        description="Perpétuos USDT-M ranqueados por tendência, volatilidade, momentum, volume, funding e sinais das estratégias."
      >
        {canTrade && (
          <Button variant="outline" onClick={refresh}>
            Atualizar agora
          </Button>
        )}
      </PageHeader>
      <Alert variant="warning">
        Informação de mercado, não recomendação. Nenhum score garante lucro; todas as ordens dos bots passam pelo
        gerenciador de risco.
      </Alert>
      {error && <Alert variant="destructive">{error}</Alert>}
      {info && <Alert>{info}</Alert>}
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <div className="space-y-1">
              <Label htmlFor="sc-search">Buscar</Label>
              <Input id="sc-search" placeholder="BTC" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="sc-trend">Tendência</Label>
              <Select id="sc-trend" value={trend} onChange={(e) => setTrend(e.target.value)}>
                <option value="">Todas</option>
                <option value="up">Alta</option>
                <option value="down">Baixa</option>
                <option value="flat">Lateral</option>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="sc-signal">Sinal da estratégia</Label>
              <Select id="sc-signal" value={signal} onChange={(e) => setSignal(e.target.value)}>
                <option value="">Qualquer</option>
                {(data?.strategies ?? []).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="sc-vol">Volume mín. (milhões USDT)</Label>
              <Input id="sc-vol" type="number" min={0} value={minVolume} onChange={(e) => setMinVolume(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="sc-score">Score mínimo</Label>
              <Input
                id="sc-score"
                type="number"
                min={0}
                max={100}
                value={minScore}
                onChange={(e) => setMinScore(e.target.value)}
              />
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            {data?.scanned_at
              ? `Último scan: ${dateTime(data.scanned_at)} · ${data.symbols_scanned} símbolos · intervalo ${data.interval}${data.testnet ? " · testnet" : ""} · ${data.duration_ms} ms`
              : "Nenhum scan ainda (a tarefa scanner.run roda periodicamente no worker Celery)."}
          </p>
          {!data ? (
            <EmptyState>Carregando...</EmptyState>
          ) : rows.length === 0 ? (
            <EmptyState>Nenhum símbolo com esses filtros.</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>#</TableHead>
                    <TableHead>Símbolo</TableHead>
                    <TableHead>Preço</TableHead>
                    <TableHead>Tendência</TableHead>
                    {COLUMNS.map((c) => (
                      <TableHead
                        key={c.key}
                        title={c.title}
                        className="cursor-pointer select-none whitespace-nowrap"
                        onClick={() => toggleSort(c.key)}
                      >
                        {c.label}
                        {sort === c.key ? (desc ? " ↓" : " ↑") : ""}
                      </TableHead>
                    ))}
                    <TableHead>Sinais</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((r) => (
                    <TableRow key={r.symbol}>
                      <TableCell className="text-xs text-muted-foreground">{r.rank}</TableCell>
                      <TableCell className="font-medium">{r.symbol}</TableCell>
                      <TableCell>{num(r.price, 6)}</TableCell>
                      <TableCell>
                        <Badge variant={TREND[r.trend]?.variant ?? "secondary"}>{TREND[r.trend]?.label ?? r.trend}</Badge>
                      </TableCell>
                      <TableCell className="font-semibold">{num(r.score, 1)}</TableCell>
                      <TableCell className={r.change_pct_24h >= 0 ? "text-green-500" : "text-red-500"}>
                        {num(r.change_pct_24h)}
                      </TableCell>
                      <TableCell>{compact.format(r.quote_volume)}</TableCell>
                      <TableCell>{num(r.adx, 1)}</TableCell>
                      <TableCell>{num(r.atr_pct)}</TableCell>
                      <TableCell className={r.momentum_pct >= 0 ? "text-green-500" : "text-red-500"}>
                        {num(r.momentum_pct)}
                      </TableCell>
                      <TableCell>{num(r.volume_spike)}</TableCell>
                      <TableCell>{num(r.funding_rate_pct, 4)}</TableCell>
                      <TableCell>{num(r.rsi, 1)}</TableCell>
                      <TableCell className="space-x-1">
                        {Object.entries(r.signals).map(([k, v]) => (
                          <Badge key={k} variant={v === "long" ? "success" : "destructive"} title={k}>
                            {k} {v === "long" ? "▲" : "▼"}
                          </Badge>
                        ))}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
