"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import type { ChartLine, ChartTrade } from "@/components/candle-chart";
import { EmptyState, PageHeader, StatusBadge } from "@/components/common";
import {
  useLivePrice,
  useLivePrices,
  useMark,
  useNet,
  useRealtimeEvent,
  useTicker,
  useWatchSymbols,
} from "@/components/realtime-provider";
import { formatPrice } from "@/components/ticker-bar";
import { TradingPanel } from "@/components/trading-panel";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import { dateTime, num, pct, pnlClass, qty, usd } from "@/lib/format";
import type { RealtimeMessage } from "@/lib/realtime";
import type { Account, AccountDashboard, Bot, Order, PositionOut, Trade } from "@/lib/types";

// lightweight-charts (~45 kB gz) loads in its own chunk, after the page shell.
const CandleChart = dynamic(() => import("@/components/candle-chart").then((m) => m.CandleChart), {
  ssr: false,
  loading: () => <div className="h-[420px] w-full" />,
});

const INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"];
const OPEN_STATUSES = new Set(["NEW", "PARTIALLY_FILLED", "PENDING"]);

interface FeedItem {
  id: string;
  ts: number;
  type: string;
  text: string;
}

function describe(msg: RealtimeMessage): string | null {
  const d = (msg.data ?? {}) as Record<string, unknown>;
  const s = (k: string) => (d[k] === undefined || d[k] === null ? "" : String(d[k]));
  switch (msg.type) {
    case "order":
      return `${s("event")} ${s("symbol")} ${s("side")} ${s("quantity")} ${s("price") && "@ " + s("price")} ${s("reason")}`;
    case "position":
      return `${s("event")} ${s("symbol")} ${s("side")} ${s("quantity")} ${s("realized_pnl") && "PnL " + s("realized_pnl")}`;
    case "manual":
      return `manual ${s("event").replace("manual.", "")} ${s("symbol")} ${s("result")} ${s("reason")}`;
    case "order_update":
      return `exchange ${s("symbol")} ${s("side")} ${s("type")} ${s("status")} ${s("last_qty")} @ ${s("last_price")}`;
    case "kill_switch":
      return `KILL SWITCH ${s("event")} ${s("reason")}`;
    case "alert":
      return `alerta: ${s("title")}`;
    case "bot":
      return `bot ${s("name") || s("bot_id")} ${s("state") || s("event")}`;
    default:
      return null;
  }
}

type LivePosition = PositionOut & { mark: number; pnl: number; roe: number };

function withLivePnl(positions: PositionOut[], prices: Record<string, number | null>): LivePosition[] {
  return positions.map((p) => {
    const mark = prices[p.symbol] ?? num(p.mark_price ?? p.entry_price);
    const q = num(p.quantity);
    const pnl = q * (mark - num(p.entry_price));
    const notional = Math.abs(q) * num(p.entry_price);
    const roe = notional > 0 ? (pnl / (notional / Math.max(p.leverage, 1))) * 100 : 0;
    return { ...p, mark, pnl, roe };
  });
}

/**
 * Live P&L of the open positions. Only the components that call this re-render on price
 * messages (and only when one of these symbols' price changes); the chart, order/event
 * tables and the rest of the page stay put.
 */
function useLivePositions(positions: PositionOut[]): { live: LivePosition[]; totalUpnl: number } {
  const prices = useLivePrices(positions.map((p) => p.symbol));
  return useMemo(() => {
    const live = withLivePnl(positions, prices);
    return { live, totalUpnl: live.reduce((acc, p) => acc + p.pnl, 0) };
  }, [positions, prices]);
}

function LiveQuote({ symbol }: { symbol: string }) {
  const ticker = useTicker(symbol);
  const mark = useMark(symbol);
  return (
    <div className="ml-auto text-right text-sm tabular-nums">
      <div className="font-semibold">{formatPrice(ticker?.c ?? null)}</div>
      <div className="text-xs text-muted-foreground">
        mark {formatPrice(mark?.p ?? null)} · funding{" "}
        {mark?.r != null ? pct(mark.r * 100, 4) : "—"}
      </div>
    </div>
  );
}

function PositionsCard({
  positions,
  canTrade,
  onSelect,
  onClose,
  onCloseAll,
}: {
  positions: PositionOut[];
  canTrade: boolean;
  onSelect: (symbol: string) => void;
  onClose: (symbol: string) => void;
  onCloseAll: () => void;
}) {
  const { live: livePositions, totalUpnl } = useLivePositions(positions);
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="text-base">
          Posições · PnL não realizado <span className={pnlClass(totalUpnl)}>{usd(totalUpnl)}</span>
        </CardTitle>
        {canTrade && livePositions.length > 0 && (
          <Button size="sm" variant="destructive" onClick={onCloseAll}>
            Fechar todas
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {livePositions.length === 0 ? (
          <p className="text-sm text-muted-foreground">Sem posições abertas.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Símbolo</TableHead>
                <TableHead>Qtd.</TableHead>
                <TableHead>Entrada</TableHead>
                <TableHead>Mark</TableHead>
                <TableHead>SL / TP</TableHead>
                <TableHead>PnL</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {livePositions.map((p) => (
                <TableRow key={p.symbol} className="cursor-pointer" onClick={() => onSelect(p.symbol)}>
                  <TableCell>
                    {p.symbol}{" "}
                    <Badge variant={num(p.quantity) > 0 ? "success" : "destructive"}>
                      {num(p.quantity) > 0 ? "LONG" : "SHORT"} {p.leverage}x
                    </Badge>
                  </TableCell>
                  <TableCell className="tabular-nums">{qty(Math.abs(num(p.quantity)))}</TableCell>
                  <TableCell className="tabular-nums">{formatPrice(num(p.entry_price))}</TableCell>
                  <TableCell className="tabular-nums">{formatPrice(p.mark)}</TableCell>
                  <TableCell className="tabular-nums text-xs">
                    {p.stop_loss ? formatPrice(num(p.stop_loss)) : "—"} /{" "}
                    {p.take_profit ? formatPrice(num(p.take_profit)) : "—"}
                  </TableCell>
                  <TableCell className={`tabular-nums ${pnlClass(p.pnl)}`}>
                    {usd(p.pnl)} ({p.roe.toFixed(2)}%)
                  </TableCell>
                  <TableCell>
                    {canTrade && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={(e) => {
                          e.stopPropagation();
                          onClose(p.symbol);
                        }}
                      >
                        Fechar
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

function AccountCard({
  account,
  dash,
  positions,
}: {
  account: Account;
  dash: AccountDashboard | null;
  positions: PositionOut[];
}) {
  const { totalUpnl } = useLivePositions(positions);
  const equity = dash ? num(dash.wallet_balance) + totalUpnl : 0;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center justify-between text-base">
          Conta <StatusBadge status={account.status} />
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-1 text-sm">
        <div className="flex justify-between">
          <span className="text-muted-foreground">Patrimônio (ao vivo)</span>
          <span className="tabular-nums">{usd(equity)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted-foreground">Saldo</span>
          <span className="tabular-nums">{usd(dash?.wallet_balance)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted-foreground">PnL realizado hoje</span>
          <span className={`tabular-nums ${pnlClass(dash?.realized_pnl_today)}`}>{usd(dash?.realized_pnl_today)}</span>
        </div>
      </CardContent>
    </Card>
  );
}

function ManualOrderCard({
  account,
  symbol,
  dash,
  positions,
  globalKill,
  canTrade,
  onDone,
}: {
  account: Account;
  symbol: string;
  dash: AccountDashboard | null;
  positions: PositionOut[];
  globalKill: boolean;
  canTrade: boolean;
  onDone: () => void;
}) {
  const marketPrice = useLivePrice(symbol);
  const { totalUpnl } = useLivePositions(positions);
  const equity = dash ? num(dash.wallet_balance) + totalUpnl : 0;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Ordem manual</CardTitle>
      </CardHeader>
      <CardContent>
        <TradingPanel
          account={account}
          symbol={symbol}
          marketPrice={marketPrice}
          equity={equity}
          globalKill={globalKill}
          canTrade={canTrade}
          onDone={onDone}
        />
      </CardContent>
    </Card>
  );
}

export default function LivePage() {
  const { workspace } = useAuth();
  const canTrade = workspace?.role === "admin" || workspace?.role === "trader";
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountId] = useState<string>("");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [symbolDraft, setSymbolDraft] = useState("BTCUSDT");
  const [interval, setChartInterval] = useState("15m");
  const [dash, setDash] = useState<AccountDashboard | null>(null);
  const [positions, setPositions] = useState<PositionOut[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [bots, setBots] = useState<Bot[]>([]);
  const [globalKill, setGlobalKill] = useState(false);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [closing, setClosing] = useState<{ kind: "one"; symbol: string } | { kind: "all" } | null>(null);
  const [busy, setBusy] = useState(false);

  const account = accounts.find((a) => a.id === accountId) ?? null;
  useNet(account ? (account.is_testnet ? "test" : "main") : null);

  useEffect(() => {
    api
      .listAccounts()
      .then((list) => {
        setAccounts(list);
        setAccountId((cur) => cur || list[0]?.id || "");
      })
      .catch((e) => setError(errorMessage(e)));
    api
      .tradingStatus()
      .then((s) => setGlobalKill(s.global_kill_switch))
      .catch(() => undefined);
  }, []);

  // Promise chain: state is only set from async callbacks (no synchronous setState inside the effect).
  const load = useCallback(() => {
    if (!accountId) return Promise.resolve();
    return Promise.all([
      api.accountDashboard(accountId),
      api.positions({ account_id: accountId }),
      api.orders({ account_id: accountId, limit: 30 }),
      api.trades({ account_id: accountId, symbol, limit: 200 }),
      api.listBots(accountId),
      api.listAccounts(),
      api.tradingStatus(),
    ])
      .then(([d, pos, ord, tr, b, accs, st]) => {
        setDash(d);
        setPositions(pos.filter((p) => num(p.quantity) !== 0));
        setOrders(ord.items);
        setTrades(tr.items);
        setBots(b);
        setAccounts(accs);
        setGlobalKill(st.global_kill_switch);
        setError(null);
      })
      .catch((e) => setError(errorMessage(e)));
  }, [accountId, symbol]);

  useEffect(() => {
    void load();
  }, [load]);

  // refresh REST state on relevant events (debounced), and keep an event feed
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useRealtimeEvent(
    ["order", "position", "manual", "account", "account_update", "order_update", "bot", "kill_switch", "alert"],
    (msg) => {
      const d = (msg.data ?? {}) as { account_id?: string | null };
      const text = describe(msg);
      if (text) {
        setFeed((f) =>
          [{ id: `${msg.ts}-${Math.random()}`, ts: msg.ts * 1000, type: msg.type, text: text.trim() }, ...f].slice(0, 50)
        );
      }
      if (d.account_id && d.account_id !== accountId && msg.type !== "kill_switch") return;
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => void load(), 400);
    }
  );

  const watched = useMemo(() => Array.from(new Set([symbol, ...positions.map((p) => p.symbol)])), [symbol, positions]);
  useWatchSymbols(watched);

  // Derived from REST state only (entry/SL/TP/limit prices do not move with the market), memoized so
  // the chart does not redo markers or price lines unless trades/positions/orders actually change.
  const lines = useMemo(() => {
    const out: ChartLine[] = [];
    const current = positions.find((p) => p.symbol === symbol);
    if (current) {
      out.push({ price: num(current.entry_price), title: "Entrada", color: "#94a3b8" });
      if (current.stop_loss) out.push({ price: num(current.stop_loss), title: "SL", color: "#ef4444", dashed: true });
      if (current.take_profit)
        out.push({ price: num(current.take_profit), title: "TP", color: "#22c55e", dashed: true });
    }
    orders
      .filter((o) => o.symbol === symbol && OPEN_STATUSES.has(o.status) && o.price)
      .forEach((o) => out.push({ price: num(o.price), title: `LIMIT ${o.side}`, color: "#3b82f6", dashed: true }));
    return out;
  }, [positions, orders, symbol]);
  const chartTrades = useMemo<ChartTrade[]>(
    () =>
      trades.map((t) => ({
        time: t.executed_at,
        side: t.side,
        price: num(t.price),
        quantity: num(t.quantity),
        realized_pnl: num(t.realized_pnl),
      })),
    [trades]
  );
  const onDone = useCallback(() => void load(), [load]);

  const doClose = async () => {
    if (!closing || !account) return;
    setBusy(true);
    try {
      if (closing.kind === "all") {
        const r = await api.closeAll(account.id);
        if (r.failed.length) setError(`Falha ao fechar: ${r.failed.join(", ")}`);
      } else {
        const r = await api.closePosition(account.id, closing.symbol);
        if (!r.ok) setError(`Fechamento rejeitado: ${r.reason}`);
      }
      await load();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
      setClosing(null);
    }
  };

  const cancelOrder = async (o: Order) => {
    if (!account) return;
    try {
      await api.cancelManualOrder(o.id, account.id);
      await load();
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  if (!accounts.length) {
    return (
      <>
        <PageHeader title="Tempo real & Trading" description="Painel em tempo real e ordens manuais" />
        <EmptyState>Conecte uma conta (paper por padrão) em Contas para usar o painel.</EmptyState>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Tempo real & Trading"
        description="Cotações, candles e posições ao vivo. Toda ordem manual passa pelo gerenciador de risco."
      >
        <Select aria-label="Conta" value={accountId} onChange={(e) => setAccountId(e.target.value)} className="h-9 w-56">
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.label} · {a.execution_mode === "live" ? "REAL" : "paper"}
              {a.is_testnet ? " · testnet" : ""}
            </option>
          ))}
        </Select>
      </PageHeader>

      {error && (
        <Alert variant="destructive" className="mb-4">
          {error}
        </Alert>
      )}
      {globalKill && (
        <Alert variant="destructive" className="mb-4">
          Kill switch global ativo: novas entradas estão bloqueadas em todas as contas.
        </Alert>
      )}

      <div className="grid gap-4 xl:grid-cols-[1fr_340px]">
        <div className="space-y-4">
          <Card>
            <CardHeader className="flex flex-row flex-wrap items-center gap-3 space-y-0 pb-2">
              <form
                className="flex items-center gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  const s = symbolDraft.trim().toUpperCase();
                  if (/^[A-Z0-9]{2,20}$/.test(s)) setSymbol(s.endsWith("USDT") ? s : `${s}USDT`);
                }}
              >
                <Label htmlFor="sym" className="sr-only">
                  Símbolo
                </Label>
                <Input
                  id="sym"
                  className="h-9 w-32"
                  value={symbolDraft}
                  onChange={(e) => setSymbolDraft(e.target.value)}
                />
                <Button size="sm" variant="outline" type="submit">
                  Ir
                </Button>
              </form>
              <div className="flex gap-1">
                {INTERVALS.map((i) => (
                  <Button key={i} size="sm" variant={i === interval ? "default" : "ghost"} onClick={() => setChartInterval(i)}>
                    {i}
                  </Button>
                ))}
              </div>
              <LiveQuote symbol={symbol} />
            </CardHeader>
            <CardContent>
              <CandleChart
                symbol={symbol}
                interval={interval}
                testnet={account?.is_testnet ?? false}
                trades={chartTrades}
                lines={lines}
              />
            </CardContent>
          </Card>

          <PositionsCard
            positions={positions}
            canTrade={canTrade}
            onSelect={setSymbol}
            onClose={(sym) => setClosing({ kind: "one", symbol: sym })}
            onCloseAll={() => setClosing({ kind: "all" })}
          />

          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Ordens recentes</CardTitle>
              </CardHeader>
              <CardContent className="max-h-80 overflow-y-auto">
                {orders.length === 0 ? (
                  <p className="text-sm text-muted-foreground">Nenhuma ordem.</p>
                ) : (
                  <Table>
                    <TableBody>
                      {orders.map((o) => (
                        <TableRow key={o.id}>
                          <TableCell className="text-xs">{dateTime(o.created_at)}</TableCell>
                          <TableCell className="text-xs">
                            {o.symbol} {o.side} {o.type}
                          </TableCell>
                          <TableCell className="text-xs tabular-nums">
                            {qty(o.quantity)}
                            {o.price ? ` @ ${o.price}` : o.avg_price ? ` @ ${o.avg_price}` : ""}
                          </TableCell>
                          <TableCell>
                            <Badge variant={o.status === "REJECTED" ? "destructive" : "outline"} title={o.reason ?? ""}>
                              {o.status}
                            </Badge>
                          </TableCell>
                          <TableCell>
                            {canTrade && OPEN_STATUSES.has(o.status) && (
                              <Button size="sm" variant="ghost" onClick={() => cancelOrder(o)}>
                                Cancelar
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
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Eventos ao vivo</CardTitle>
              </CardHeader>
              <CardContent className="max-h-80 space-y-1 overflow-y-auto text-xs">
                {feed.length === 0 ? (
                  <p className="text-muted-foreground">Aguardando ordens, execuções e alertas…</p>
                ) : (
                  feed.map((f) => (
                    <div key={f.id} className="flex gap-2">
                      <span className="shrink-0 text-muted-foreground">
                        {new Date(f.ts).toLocaleTimeString("pt-BR")}
                      </span>
                      <Badge variant={f.type === "kill_switch" ? "destructive" : "outline"} className="shrink-0">
                        {f.type}
                      </Badge>
                      <span className="break-all">{f.text}</span>
                    </div>
                  ))
                )}
              </CardContent>
            </Card>
          </div>
        </div>

        <div className="space-y-4">
          {account && <AccountCard account={account} dash={dash} positions={positions} />}
          {account && (
            <ManualOrderCard
              account={account}
              symbol={symbol}
              dash={dash}
              positions={positions}
              globalKill={globalKill}
              canTrade={canTrade}
              onDone={onDone}
            />
          )}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Bots da conta</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {bots.length === 0 ? (
                <p className="text-muted-foreground">Nenhum bot.</p>
              ) : (
                bots.map((b) => (
                  <div key={b.id} className="flex items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate">{b.name}</p>
                      <p className="text-xs text-muted-foreground">
                        {b.strategy_key} · {b.symbol} · {b.interval}
                        {b.last_tick_at ? ` · ${dateTime(b.last_tick_at)}` : ""}
                      </p>
                      {b.last_error && <p className="truncate text-xs text-red-500">{b.last_error}</p>}
                    </div>
                    <StatusBadge status={b.state} />
                  </div>
                ))
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <Dialog open={closing !== null} onOpenChange={(o) => !o && setClosing(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {closing?.kind === "all" ? "Fechar todas as posições?" : `Fechar posição ${closing?.kind === "one" ? closing.symbol : ""}?`}
            </DialogTitle>
            <DialogDescription>
              Ordens a mercado reduce-only{account?.execution_mode === "live" ? " na Binance (conta REAL)" : " (simulado)"};
              ordens abertas do(s) símbolo(s) são canceladas. Permitido mesmo com kill switch.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setClosing(null)} disabled={busy}>
              Cancelar
            </Button>
            <Button variant="destructive" onClick={doClose} disabled={busy}>
              {busy ? "Fechando…" : "Confirmar fechamento"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
