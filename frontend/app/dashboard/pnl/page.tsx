"use client";

import { useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader } from "@/components/common";
import { CumulativeChart, PnlBarChart } from "@/components/pnl-charts-lazy";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import { dateOnly, num, pnlClass, usd } from "@/lib/format";
import type { Account, PnlGroupBy, PnlPeriod, PnlReport } from "@/lib/types";

const PERIODS: Record<PnlPeriod, string> = { day: "Dia", week: "Semana", month: "Mês" };
const GROUPS: Record<PnlGroupBy, string> = {
  none: "Total",
  account: "Conta",
  bot: "Bot",
  strategy: "Estratégia",
  symbol: "Símbolo",
};
const MAX_SERIES = 7;

function toIsoStart(d: string) {
  return d ? new Date(`${d}T00:00:00Z`).toISOString() : undefined;
}
function toIsoEnd(d: string) {
  return d ? new Date(new Date(`${d}T00:00:00Z`).getTime() + 86_400_000).toISOString() : undefined;
}

function periodLabel(iso: string, period: PnlPeriod) {
  const [y, m] = iso.split("-");
  return period === "month" ? `${m}/${y}` : dateOnly(iso);
}

export default function PnlPage() {
  const { workspace } = useAuth();
  const [period, setPeriod] = useState<PnlPeriod>("day");
  const [groupBy, setGroupBy] = useState<PnlGroupBy>("none");
  const [accountId, setAccountId] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [report, setReport] = useState<PnlReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const [delimiter, setDelimiter] = useState<"comma" | "semicolon">("semicolon");

  useEffect(() => {
    api
      .listAccounts()
      .then(setAccounts)
      .catch(() => undefined);
  }, [workspace?.id]);

  useEffect(() => {
    let cancelled = false;
    api
      .pnl({
        period,
        group_by: groupBy,
        account_id: accountId || undefined,
        start: toIsoStart(start),
        end: toIsoEnd(end),
      })
      .then((r) => {
        if (cancelled) return;
        setReport(r);
        setError(null);
      })
      .catch((e) => !cancelled && setError(errorMessage(e)));
    return () => {
      cancelled = true;
    };
  }, [period, groupBy, accountId, start, end, workspace?.id]);

  const chart = useMemo(() => {
    if (!report) return { rows: [], series: [] as string[], cumulative: [] };
    const top = report.groups.slice(0, MAX_SERIES).map((g) => g.group);
    const labelOf = new Map(report.groups.map((g) => [g.group, g.label]));
    const hasOthers = report.groups.length > MAX_SERIES;
    const series = [...top.map((k) => labelOf.get(k) ?? k), ...(hasOthers ? ["Outros"] : [])];
    const byPeriod = new Map<string, Record<string, number | string> & { period: string }>();
    for (const b of report.buckets) {
      const key = b.period_start;
      const row = byPeriod.get(key) ?? { period: periodLabel(key, report.period), _iso: key };
      const name = top.includes(b.group) ? (labelOf.get(b.group) ?? b.group) : "Outros";
      row[name] = Math.round(((Number(row[name]) || 0) + num(b.net_pnl)) * 100) / 100;
      byPeriod.set(key, row);
    }
    const rows = [...byPeriod.values()].sort((a, b) => String(a._iso).localeCompare(String(b._iso)));
    let acc = 0;
    const cumulative = rows.map((r) => {
      acc += series.reduce((s, k) => s + (Number(r[k]) || 0), 0);
      return { period: r.period, acumulado: Math.round(acc * 100) / 100 };
    });
    return { rows, series, cumulative };
  }, [report]);

  const exportParams = {
    account_id: accountId || undefined,
    start: toIsoStart(start),
    end: toIsoEnd(end),
    delimiter,
  };

  async function exportCsv(kind: "trades" | "income") {
    setExporting(kind);
    setError(null);
    try {
      await api.downloadCsv(kind, kind === "income" ? { ...exportParams, period } : exportParams);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setExporting(null);
    }
  }

  const t = report?.totals;
  const otherFees = t ? Object.entries(t.fees_other) : [];

  return (
    <div className="space-y-6">
      <PageHeader title="PnL & relatórios" description="PnL realizado por período (UTC), por conta, bot ou estratégia." />
      {error && <Alert variant="destructive">{error}</Alert>}

      <Card>
        <CardContent className="grid gap-3 pt-6 sm:grid-cols-2 lg:grid-cols-5">
          <div className="space-y-1">
            <Label htmlFor="pnl-period">Período</Label>
            <Select id="pnl-period" value={period} onChange={(e) => setPeriod(e.target.value as PnlPeriod)}>
              {Object.entries(PERIODS).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="pnl-group">Agrupar por</Label>
            <Select id="pnl-group" value={groupBy} onChange={(e) => setGroupBy(e.target.value as PnlGroupBy)}>
              {Object.entries(GROUPS).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="pnl-account">Conta</Label>
            <Select id="pnl-account" value={accountId} onChange={(e) => setAccountId(e.target.value)}>
              <option value="">Todas as contas</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.label} ({a.execution_mode === "live" ? "real" : "paper"})
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="pnl-start">De</Label>
            <Input id="pnl-start" type="date" value={start} max={end || undefined} onChange={(e) => setStart(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="pnl-end">Até</Label>
            <Input id="pnl-end" type="date" value={end} min={start || undefined} onChange={(e) => setEnd(e.target.value)} />
          </div>
          <p className="text-xs text-muted-foreground sm:col-span-2 lg:col-span-5">
            Sem datas: últimos 30 dias (dia), 26 semanas (semana) ou 12 meses (mês).
          </p>
        </CardContent>
      </Card>

      <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        {[
          { label: "PnL realizado", value: t?.realized_pnl },
          { label: "Taxas (USDT)", value: t ? String(-num(t.fees)) : undefined },
          { label: "PnL líquido", value: t?.net_pnl },
        ].map((s) => (
          <Card key={s.label}>
            <CardContent className="pt-6">
              <p className="text-xs text-muted-foreground">{s.label}</p>
              <p className={`text-2xl font-semibold tabular-nums ${pnlClass(s.value)}`}>{t ? usd(s.value) : "—"}</p>
            </CardContent>
          </Card>
        ))}
        <Card>
          <CardContent className="pt-6">
            <p className="text-xs text-muted-foreground">Execuções</p>
            <p className="text-2xl font-semibold tabular-nums">{t ? t.trades : "—"}</p>
            {otherFees.length > 0 && (
              <p className="text-xs text-muted-foreground">
                Taxas em outros ativos: {otherFees.map(([k, v]) => `${num(v).toLocaleString("pt-BR")} ${k}`).join(", ")}
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">PnL líquido por {PERIODS[period].toLowerCase()}</CardTitle>
          <CardDescription>
            {groupBy === "none" ? "Total do workspace." : `Empilhado por ${GROUPS[groupBy].toLowerCase()}.`} PnL líquido =
            realizado − taxas em USDT. Financiamento (funding) não incluído.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {!report ? (
            <EmptyState>Carregando...</EmptyState>
          ) : report.buckets.length === 0 ? (
            <EmptyState>Nenhuma execução no período.</EmptyState>
          ) : (
            <>
              <PnlBarChart data={chart.rows} series={chart.series} />
              <CumulativeChart data={chart.cumulative} />
            </>
          )}
        </CardContent>
      </Card>

      {report && report.groups.length > 0 && groupBy !== "none" && (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Por {GROUPS[groupBy].toLowerCase()}</CardTitle>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{GROUPS[groupBy]}</TableHead>
                  <TableHead className="text-right">Execuções</TableHead>
                  <TableHead className="text-right">PnL realizado</TableHead>
                  <TableHead className="text-right">Taxas</TableHead>
                  <TableHead className="text-right">PnL líquido</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.groups.map((g) => (
                  <TableRow key={g.group}>
                    <TableCell className="font-medium">{g.label}</TableCell>
                    <TableCell className="text-right">{g.trades}</TableCell>
                    <TableCell className={`text-right tabular-nums ${pnlClass(g.realized_pnl)}`}>{usd(g.realized_pnl)}</TableCell>
                    <TableCell className="text-right tabular-nums">{usd(g.fees)}</TableCell>
                    <TableCell className={`text-right tabular-nums ${pnlClass(g.net_pnl)}`}>{usd(g.net_pnl)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Exportar para contabilidade (CSV)</CardTitle>
          <CardDescription>
            Usa os filtros de conta e datas acima (padrão: últimos 12 meses). Datas em UTC, valores com ponto decimal.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor="csv-delim">Separador</Label>
              <Select
                id="csv-delim"
                value={delimiter}
                onChange={(e) => setDelimiter(e.target.value as typeof delimiter)}
                className="w-56"
              >
                <option value="semicolon">Ponto e vírgula (Excel pt-BR)</option>
                <option value="comma">Vírgula</option>
              </Select>
            </div>
            <Button onClick={() => exportCsv("trades")} disabled={exporting !== null}>
              {exporting === "trades" ? "Exportando..." : "Execuções (trades)"}
            </Button>
            <Button variant="outline" onClick={() => exportCsv("income")} disabled={exporting !== null}>
              {exporting === "income" ? "Exportando..." : `PnL e taxas por ${PERIODS[period].toLowerCase()} e conta`}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Os arquivos ajudam a apurar resultados e a guardar o histórico, mas não substituem a orientação de um
            contador: regras de tributação e de declaração mudam e devem ser confirmadas com um profissional.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
