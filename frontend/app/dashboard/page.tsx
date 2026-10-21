"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader, StatusBadge } from "@/components/common";
import type { EquityPoint } from "@/components/equity-chart";
import { EquityChart } from "@/components/equity-chart-lazy";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import { dateTime, num, pct, pnlClass, qty, usd } from "@/lib/format";
import type { Account, AccountDashboard, Bot } from "@/lib/types";

// Mock data shown only when the workspace has no accounts yet.
const MOCK: AccountDashboard = {
  account_id: "mock",
  source: "paper",
  status: "stopped",
  status_reason: null,
  execution_mode: "paper",
  is_testnet: true,
  wallet_balance: "10250.42",
  available_balance: "9800",
  unrealized_pnl: "33.70",
  equity: "10284.12",
  realized_pnl_today: "125.37",
  total_pnl: "284.12",
  open_positions: 2,
  positions: [
    { symbol: "BTCUSDT", quantity: "0.01", entry_price: "64250", mark_price: "68460", unrealized_pnl: "42.10", leverage: 2, stop_loss: "62000" },
    { symbol: "ETHUSDT", quantity: "-0.2", entry_price: "3120.5", mark_price: "3162.5", unrealized_pnl: "-8.40", leverage: 2, stop_loss: "3250" },
  ],
  peak_equity: "10300",
  day_start_equity: "10150",
  drawdown_pct: 0.15,
  error: null,
};
const MOCK_EQUITY: EquityPoint[] = Array.from({ length: 30 }, (_, i) => ({
  date: `D${i + 1}`,
  equity: Math.round((10000 + i * 9 + Math.sin(i / 2) * 60) * 100) / 100,
}));

export default function DashboardPage() {
  const { workspace } = useAuth();
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [dash, setDash] = useState<AccountDashboard | null>(null);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [bots, setBots] = useState<Bot[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listAccounts()
      .then((list) => {
        setAccounts(list);
        if (list.length) setSelected((s) => s || list[0].id);
      })
      .catch((e) => setError(errorMessage(e)));
  }, [workspace?.id]);

  const load = useCallback(async () => {
    if (!selected) return;
    try {
      const [d, eq, b] = await Promise.all([
        api.accountDashboard(selected),
        api.equity({ account_id: selected, limit: 500 }),
        api.listBots(selected),
      ]);
      setDash(d);
      setEquity(eq.map((p) => ({ date: dateTime(p.taken_at), equity: num(p.equity) })));
      setBots(b);
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [selected]);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const isMock = accounts !== null && accounts.length === 0;
  const data = isMock ? MOCK : dash;
  const curve = isMock ? MOCK_EQUITY : equity;

  return (
    <div className="space-y-6">
      <PageHeader title="Dashboard" description={workspace ? `Workspace: ${workspace.name}` : undefined}>
        {accounts && accounts.length > 0 && (
          <Select value={selected} onChange={(e) => setSelected(e.target.value)} className="w-64">
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label} ({a.execution_mode === "live" ? "real" : "paper"}
                {a.is_testnet ? ", testnet" : ""})
              </option>
            ))}
          </Select>
        )}
      </PageHeader>

      {isMock && (
        <Alert variant="warning">
          Dados de exemplo — nenhuma conta conectada neste workspace.{" "}
          <Link href="/dashboard/accounts" className="underline">
            Conecte uma conta Binance
          </Link>{" "}
          (o modo paper é o padrão; nenhuma ordem real é enviada).
        </Alert>
      )}
      {error && <Alert variant="destructive">{error}</Alert>}
      {data?.error && <Alert variant="warning">{data.error}</Alert>}

      {data && (
        <>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <StatusBadge status={data.status} />
            <Badge variant={data.execution_mode === "live" ? "destructive" : "secondary"}>
              {data.execution_mode === "live" ? "Modo REAL" : "Paper trading"}
            </Badge>
            {data.is_testnet && <Badge variant="outline">Testnet</Badge>}
            <span className="text-muted-foreground">Fonte: {data.source}</span>
            {data.status_reason && <span className="text-red-400">({data.status_reason})</span>}
          </div>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Card>
              <CardHeader>
                <CardDescription>Patrimônio (USDT)</CardDescription>
                <CardTitle>{usd(data.equity)}</CardTitle>
              </CardHeader>
              <CardContent className="text-sm text-muted-foreground">
                Saldo: {usd(data.wallet_balance)} · Disponível: {usd(data.available_balance)}
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardDescription>PnL realizado hoje</CardDescription>
                <CardTitle className={pnlClass(data.realized_pnl_today)}>{usd(data.realized_pnl_today)}</CardTitle>
              </CardHeader>
              <CardContent className="text-sm text-muted-foreground">
                Total: <span className={pnlClass(data.total_pnl)}>{data.total_pnl === null ? "—" : usd(data.total_pnl)}</span>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardDescription>PnL não realizado</CardDescription>
                <CardTitle className={pnlClass(data.unrealized_pnl)}>{usd(data.unrealized_pnl)}</CardTitle>
              </CardHeader>
              <CardContent className="text-sm text-muted-foreground">Drawdown: {pct(data.drawdown_pct)}</CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardDescription>Posições abertas</CardDescription>
                <CardTitle>{data.open_positions}</CardTitle>
              </CardHeader>
              <CardContent className="text-sm text-muted-foreground">
                Bots ativos: {isMock ? 0 : bots.filter((b) => b.state === "running").length}/{isMock ? 0 : bots.length}
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Curva de patrimônio</CardTitle>
              <CardDescription>
                {isMock ? "Exemplo" : "Snapshots periódicos (tarefa de equity a cada 5 min)"}
              </CardDescription>
            </CardHeader>
            <CardContent>
              {curve.length ? <EquityChart data={curve} /> : <EmptyState>Sem snapshots ainda.</EmptyState>}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Posições abertas</CardTitle>
            </CardHeader>
            <CardContent>
              {data.positions.length === 0 ? (
                <EmptyState>Nenhuma posição aberta.</EmptyState>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Símbolo</TableHead>
                      <TableHead>Lado</TableHead>
                      <TableHead>Qtd.</TableHead>
                      <TableHead>Entrada</TableHead>
                      <TableHead>Marcação</TableHead>
                      <TableHead>Stop</TableHead>
                      <TableHead>PnL</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.positions.map((p) => (
                      <TableRow key={p.symbol}>
                        <TableCell>{p.symbol}</TableCell>
                        <TableCell>{num(p.quantity) > 0 ? "LONG" : "SHORT"}</TableCell>
                        <TableCell>{qty(Math.abs(num(p.quantity)))}</TableCell>
                        <TableCell>{qty(p.entry_price)}</TableCell>
                        <TableCell>{p.mark_price ? qty(p.mark_price) : "—"}</TableCell>
                        <TableCell>{p.stop_loss ? qty(p.stop_loss) : "—"}</TableCell>
                        <TableCell className={pnlClass(p.unrealized_pnl)}>{usd(p.unrealized_pnl)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
          {!isMock && (
            <div className="flex gap-2">
              <Button variant="outline" onClick={load}>
                Atualizar
              </Button>
              <Button variant="outline" asChild>
                <Link href="/dashboard/bots">Gerenciar bots</Link>
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
